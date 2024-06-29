import contextlib
import math

import torch
import tqdm

import comfy
from comfy.k_diffusion.sampling import (
    get_ancestral_step,
    to_d,
)

from .res_support import _de_second_order
from .utils import find_first_unsorted, extract_pred

HAVE_TDE = HAVE_TODE = False

with contextlib.suppress(ImportError):
    import torchdiffeq as tde

    HAVE_TDE = True

with contextlib.suppress(ImportError):
    import torchode as tode

    HAVE_TODE = True


class SamplerResult:
    def __init__(
        self,
        ss,
        sampler,
        x,
        strength=None,
        *,
        sigma=None,
        sigma_next=None,
        s_noise=None,
        noise_sampler=None,
        final=True,
    ):
        self.x = x
        self.sampler = sampler
        self.strength = strength if strength is not None else ss.sigma_up
        self.s_noise = s_noise if s_noise is not None else sampler.s_noise
        self.sigma = sigma if sigma is not None else ss.sigma
        self.sigma_next = sigma_next if sigma_next is not None else ss.sigma_next
        self.noise_sampler = noise_sampler if noise_sampler else sampler.noise_sampler
        self.final = final

    def get_noise(self, scaled=True):
        return self.noise_sampler(
            self.sigma, self.sigma_next, out_hw=self.x.shape[-2:]
        ).mul_(self.noise_scale if scaled else 1.0)

    @property
    def noise_scale(self):
        return self.strength * self.s_noise

    def noise_x(self, x=None, scale=1.0):
        if x is None:
            x = self.x
        else:
            self.x = x
        if self.sigma_next == 0 or self.noise_scale == 0:
            return x
        self.x = x + self.get_noise() * scale
        return self.x


class CFGPPStepMixin:
    def init_cfgpp(self, /, cfgpp_scale=0.0):
        self.cfgpp_scale = 0.0 if not self.allow_cfgpp else cfgpp_scale

    def to_d(self, mr, **kwargs):
        return mr.to_d(cfgpp_scale=self.cfgpp_scale, **kwargs)


class SingleStepSampler(CFGPPStepMixin):
    name = None
    self_noise = 0
    model_calls = 0
    allow_cfgpp = False

    def __init__(
        self,
        *,
        noise_sampler=None,
        substeps=1,
        s_noise=1.0,
        eta=1.0,
        dyn_eta_start=None,
        dyn_eta_end=None,
        weight=1.0,
        **kwargs,
    ):
        self.s_noise = s_noise
        self.eta = eta
        self.dyn_eta_start = dyn_eta_start
        self.dyn_eta_end = dyn_eta_end
        self.noise_sampler = noise_sampler
        self.weight = weight
        self.substeps = substeps
        self.init_cfgpp(cfgpp_scale=kwargs.pop("cfgpp_scale", 0.0))
        self.options = kwargs

    def step(self, x, ss):
        raise NotImplementedError

    # Euler - based on original ComfyUI implementation
    def euler_step(self, x, ss):
        sigma_down, sigma_up = ss.get_ancestral_step(self.get_dyn_eta(ss))
        d = self.to_d(ss.hcur)
        dt = sigma_down - ss.sigma
        return (yield from self.result(ss, x + d * dt, sigma_up))

    def denoised_result(self, ss, **kwargs):
        return (
            yield SamplerResult(ss, self, ss.denoised, ss.sigma.new_zeros(1), **kwargs)
        )

    def result(self, ss, x, noise_scale=None, **kwargs):
        return (yield SamplerResult(ss, self, x, noise_scale, **kwargs))

    def ancestralize_result(self, ss, x):
        eta = self.get_dyn_eta(ss)
        if ss.sigma_next == 0 or eta == 0:
            return (yield from self.result(ss, x, ss.sigma_next.new_zeros(1)))
        sd, su = ss.get_ancestral_step(self.get_dyn_eta(ss))
        out_d, out_n = extract_pred(ss.hcur.x, x, ss.sigma, ss.sigma_next)
        return (yield from self.result(ss, out_d + out_n * sd, su))

    def __str__(self):
        return f"<SS({self.name}): s_noise={self.s_noise}, eta={self.eta}>"

    def get_dyn_value(self, ss, start, end):
        if None in (start, end):
            return 1.0
        if start == end:
            return start
        main_idx = getattr(ss, "main_idx", ss.idx)
        main_sigmas = getattr(ss, "main_sigmas", ss.sigmas)
        step_pct = main_idx / (len(main_sigmas) - 1)
        dd_diff = end - start
        return start + dd_diff * step_pct

    def get_dyn_eta(self, ss):
        return self.eta * self.get_dyn_value(ss, self.dyn_eta_start, self.dyn_eta_end)

    def max_noise_samples(self):
        return (1 + self.self_noise) * self.substeps


class HistorySingleStepSampler(SingleStepSampler):
    default_history_limit, max_history = 0, 0

    def __init__(self, *args, history_limit=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.history_limit = min(
            self.max_history,
            max(
                0,
                self.default_history_limit if history_limit is None else history_limit,
            ),
        )

    def available_history(self, ss):
        return max(
            0, min(ss.idx, self.history_limit, self.max_history, len(ss.hist) - 1)
        )


class ReversibleSingleStepSampler(HistorySingleStepSampler):
    def __init__(
        self,
        *,
        reversible_scale=1.0,
        reta=1.0,
        dyn_reta_start=None,
        dyn_reta_end=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.reversible_scale = reversible_scale
        self.reta = reta
        self.dyn_reta_start = dyn_reta_start
        self.dyn_reta_end = dyn_reta_end

    def get_dyn_reta(self, ss):
        return self.reta * self.get_dyn_value(
            ss, self.dyn_reta_start, self.dyn_reta_end
        )


class DPMPPStepMixin:
    @staticmethod
    def sigma_fn(t):
        return t.neg().exp()

    @staticmethod
    def t_fn(t):
        return t.log().neg()


class MinSigmaStepMixin:
    @staticmethod
    def adjust_step(sigma, min_sigma, threshold=5e-04):
        if min_sigma - sigma > threshold:
            return sigma.clamp(min=min_sigma)
        return sigma

    def adjusted_step(self, ss, sn, result, mcc, sigma_up):
        if sn == ss.sigma_next:
            return sigma_up, result
        # FIXME: Make sure we're noising from the right sigma.
        result = yield from self.result(
            ss, result, sigma_up, sigma=ss.sigma, sigma_next=sn, final=False
        )
        mr = ss.model(result, sn, model_call_idx=mcc)
        dt = ss.sigma_next - sn
        result = result + self.to_d(mr) * dt
        sigma_up *= 0
        return sigma_up, result


class EulerStep(SingleStepSampler):
    name = "euler"
    allow_cfgpp = True
    step = SingleStepSampler.euler_step


class CycleSingleStepSampler(SingleStepSampler):
    def __init__(self, *, cycle_pct=0.25, **kwargs):
        super().__init__(**kwargs)
        self.cycle_pct = cycle_pct

    def get_cycle_scales(self, sigma_next):
        keep_scale = sigma_next * (1.0 - self.cycle_pct)
        add_scale = ((sigma_next**2.0 - keep_scale**2.0) ** 0.5) * (
            0.95 + 0.25 * self.cycle_pct
        )
        # print(f">> keep={keep_scale}, add={add_scale}")
        return keep_scale, add_scale


class EulerCycleStep(CycleSingleStepSampler):
    name = "euler_cycle"
    allow_cfgpp = True

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.denoised_result(ss))
        d = self.to_d(ss.hcur)
        keep_scale, add_scale = self.get_cycle_scales(ss.sigma_next)
        yield from self.result(ss, ss.denoised + d * keep_scale, add_scale)


class DPMPP2MStep(HistorySingleStepSampler, DPMPPStepMixin):
    name = "dpmpp_2m"
    default_history_limit, max_history = 1, 1

    def step(self, x, ss):
        s, sn = ss.sigma, ss.sigma_next
        if sn == 0:
            return (yield from self.euler_step(x, ss))
        t, t_next = self.t_fn(s), self.t_fn(sn)
        h = t_next - t
        st, st_next = self.sigma_fn(t), self.sigma_fn(t_next)
        if self.available_history(ss) > 0:
            h_last = t - self.t_fn(ss.sigma_prev)
            r = h_last / h
            denoised, old_denoised = ss.denoised, ss.hprev.denoised
            denoised_d = (1 + 1 / (2 * r)) * denoised - (1 / (2 * r)) * old_denoised
        else:
            denoised_d = ss.denoised
        yield from self.ancestralize_result(
            ss, (st_next / st) * x - (-h).expm1() * denoised_d
        )


class DPMPP2MSDEStep(HistorySingleStepSampler):
    name = "dpmpp_2m_sde"
    default_history_limit, max_history = 1, 1

    def __init__(self, *, solver_type="midpoint", **kwargs):
        super().__init__(**kwargs)
        self.solver_type = solver_type

    def step(self, x, ss):
        denoised = ss.denoised
        # DPM-Solver++(2M) SDE
        t, s = -ss.sigma.log(), -ss.sigma_next.log()
        h = s - t
        eta_h = self.get_dyn_eta(ss) * h

        x = (
            ss.sigma_next / ss.sigma * (-eta_h).exp() * x
            + (-h - eta_h).expm1().neg() * denoised
        )
        noise_strength = ss.sigma_next * (-2 * eta_h).expm1().neg().sqrt()
        if ss.sigma_next == 0 or self.available_history(ss) == 0:
            return (yield from self.result(ss, x, noise_strength))
        h_last = (-ss.sigma.log()) - (-ss.sigma_prev.log())
        r = h_last / h
        old_denoised = ss.hprev.denoised
        if self.solver_type == "heun":
            x = x + (
                ((-h - eta_h).expm1().neg() / (-h - eta_h) + 1)
                * (1 / r)
                * (denoised - old_denoised)
            )
        elif self.solver_type == "midpoint":
            x = x + 0.5 * (-h - eta_h).expm1().neg() * (1 / r) * (
                denoised - old_denoised
            )
        yield from self.result(ss, x, noise_strength)


class DPMPP3MSDEStep(HistorySingleStepSampler):
    name = "dpmpp_3m_sde"
    default_history_limit, max_history = 2, 2

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.denoised_result(ss))
        denoised = ss.denoised
        # if ss.sigma_next == 0:
        #     return denoised, 0
        t, s = -ss.sigma.log(), -ss.sigma_next.log()
        h = s - t
        eta = self.get_dyn_eta(ss)
        h_eta = h * (eta + 1)

        x = torch.exp(-h_eta) * x + (-h_eta).expm1().neg() * denoised
        noise_strength = ss.sigma_next * (-2 * h * eta).expm1().neg().sqrt()
        ah = self.available_history(ss)
        if ah == 0:
            return (yield from self.result(ss, x, noise_strength))
        hist = ss.hist
        h_1 = (-ss.sigma.log()) - (-ss.sigma_prev.log())
        denoised_1 = hist[-2].denoised
        if ah == 1:
            r = h_1 / h
            d = (denoised - denoised_1) / r
            phi_2 = h_eta.neg().expm1() / h_eta + 1
            x = x + phi_2 * d
        else:  # 2+ history items available
            h_2 = (-ss.sigma_prev.log()) - (-ss.sigmas[ss.idx - 2].log())
            denoised_2 = hist[-3].denoised
            r0 = h_1 / h
            r1 = h_2 / h
            d1_0 = (denoised - denoised_1) / r0
            d1_1 = (denoised_1 - denoised_2) / r1
            d1 = d1_0 + (d1_0 - d1_1) * r0 / (r0 + r1)
            d2 = (d1_0 - d1_1) / (r0 + r1)
            phi_2 = h_eta.neg().expm1() / h_eta + 1
            phi_3 = phi_2 / h_eta - 0.5
            x = x + phi_2 * d1 - phi_3 * d2
        yield from self.result(ss, x, noise_strength)


# Based on original implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
class ReversibleHeunStep(ReversibleSingleStepSampler):
    name = "reversible_heun"
    model_calls = 1
    allow_cfgpp = True

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        sigma_down, sigma_up = ss.get_ancestral_step(self.get_dyn_eta(ss))
        sigma_down_reversible, _sigma_up_reversible = ss.get_ancestral_step(
            self.get_dyn_reta(ss)
        )
        dt = sigma_down - ss.sigma
        dt_reversible = sigma_down_reversible - ss.sigma

        # Calculate the derivative using the model
        d = self.to_d(ss.hcur)

        # Predict the sample at the next sigma using Euler step
        x_pred = x + d * dt

        # Denoised sample at the next sigma
        mr_next = ss.model(x_pred, sigma_down, model_call_idx=1)

        # Calculate the derivative at the next sigma
        d_next = self.to_d(mr_next)

        # Update the sample using the Reversible Heun formula
        correction = dt_reversible**2 * (d_next - d) / 4
        x = x + (dt * (d + d_next) / 2) - correction * self.reversible_scale
        yield from self.result(ss, x, sigma_up)


# Based on original implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
class ReversibleHeun1SStep(ReversibleSingleStepSampler):
    name = "reversible_heun_1s"
    model_calls = 1
    default_history_limit, max_history = 1, 1
    allow_cfgpp = True

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        ah = self.available_history(ss)
        s = ss.sigma
        # Reversible Heun-inspired update (first-order)
        sd, su = ss.get_ancestral_step(self.get_dyn_eta(ss))
        sdr, _sur = ss.get_ancestral_step(self.get_dyn_reta(ss))
        dt, dtr = sd - s, sdr - s
        eff_x = ss.hist[-1].x if ah > 0 else x

        # Calculate the derivative using the model
        d_prev = self.to_d(
            ss.hist[-2] if ah > 0 else ss.model(eff_x, s, model_call_idx=1),
            x=eff_x,
            sigma=s,
        )

        # Predict the sample at the next sigma using Euler step
        x_pred = eff_x + d_prev * dt

        # Calculate the derivative at the next sigma
        d_next = self.to_d(ss.hcur, x=x_pred, sigma=sd)

        # Update the sample using the Reversible Heun formula
        correction = dtr**2 * (d_next - d_prev) / 4
        x = x + (dt * (d_prev + d_next) / 2) - correction * self.reversible_scale
        yield from self.result(ss, x, su)


# Based on original implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
class RESStep(SingleStepSampler):
    name = "res"
    model_calls = 1

    def __init__(self, *, res_simple_phi=False, res_c2=0.5, **kwargs):
        super().__init__(**kwargs)
        self.simple_phi = res_simple_phi
        self.c2 = res_c2

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        eta = self.get_dyn_eta(ss)
        sigma_down, sigma_up = ss.get_ancestral_step(eta)
        denoised = ss.denoised
        lam_next = sigma_down.log().neg() if eta != 0 else ss.sigma_next.log().neg()
        lam = ss.sigma.log().neg()

        h = lam_next - lam
        a2_1, b1, b2 = _de_second_order(
            h=h, c2=self.c2, simple_phi_calc=self.simple_phi
        )

        c2_h = 0.5 * h

        x_2 = math.exp(-c2_h) * x + a2_1 * h * denoised
        lam_2 = lam + c2_h
        sigma_2 = lam_2.neg().exp()

        denoised2 = ss.model(x_2, sigma_2, model_call_idx=1).denoised

        x = math.exp(-h) * x + h * (b1 * denoised + b2 * denoised2)
        yield from self.result(ss, x, sigma_up)


# Based on original implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
class TrapezoidalStep(SingleStepSampler):
    name = "trapezoidal"
    model_calls = 1
    allow_cfgpp = True

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        sigma_down, sigma_up = ss.get_ancestral_step(self.get_dyn_eta(ss))

        # Calculate the derivative using the model
        d_i = self.to_d(ss.hcur)

        # Predict the sample at the next sigma using Euler step
        x_pred = x + d_i * ss.dt

        # Denoised sample at the next sigma
        mr_next = ss.model(x_pred, ss.sigma_next, model_call_idx=1)

        # Calculate the derivative at the next sigma
        d_next = self.to_d(mr_next)
        dt_2 = sigma_down - ss.sigma

        # Update the sample using the Trapezoidal rule
        x = x + dt_2 * (d_i + d_next) / 2
        yield from self.result(ss, x, sigma_up)


class TrapezoidalCycleStep(CycleSingleStepSampler):
    name = "trapezoidal_cycle"
    model_calls = 1
    allow_cfgpp = True

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.denoised_result(ss))

        # Calculate the derivative using the model
        d_i = self.to_d(ss.hcur)

        # Predict the sample at the next sigma using Euler step
        x_pred = x + d_i * ss.dt

        # Denoised sample at the next sigma
        mr_next = ss.model(x_pred, ss.sigma_next, model_call_idx=1)

        # Calculate the derivative at the next sigma
        d_next = self.to_d(mr_next)

        # Update the sample using the Trapezoidal rule
        keep_scale, add_scale = self.get_cycle_scales(ss.sigma_next)
        noise_pred = (d_i + d_next) * 0.5  # Combined noise prediction
        denoised_pred = x - noise_pred * ss.sigma  # Denoised prediction
        yield from self.result(ss, denoised_pred + noise_pred * keep_scale, add_scale)


# Based on original implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
class BogackiStep(ReversibleSingleStepSampler):
    name = "bogacki"
    reversible = False
    model_calls = 2
    allow_cfgpp = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.reversible:
            self.reversible_scale = 0

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        s = ss.sigma
        sd, su = ss.get_ancestral_step(self.get_dyn_eta(ss))
        reta = self.get_dyn_reta(ss) if self.reversible else 0.0
        sdr, _sur = ss.get_ancestral_step(reta)
        dt, dtr = sd - s, sdr - s

        # Calculate the derivative using the model
        d = self.to_d(ss.hcur)

        # Bogacki-Shampine steps
        k1 = d * dt
        k2 = self.to_d(ss.model(x + k1 / 2, s + dt / 2, model_call_idx=1)) * dt
        k3 = (
            self.to_d(
                ss.model(x + 3 * k1 / 4 + k2 / 4, s + 3 * dt / 4, model_call_idx=2)
            )
            * dt
        )

        # Reversible correction term (inspired by Reversible Heun)
        correction = dtr**2 * (k3 - k2) / 6

        # Update the sample
        x = (x + 2 * k1 / 9 + k2 / 3 + 4 * k3 / 9) - correction * self.reversible_scale
        yield from self.result(ss, x, su)


class ReversibleBogackiStep(BogackiStep):
    name = "reversible_bogacki"
    reversible = True


# Based on original implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
class RK4Step(SingleStepSampler):
    name = "rk4"
    model_calls = 3
    allow_cfgpp = True

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        sigma_down, sigma_up = ss.get_ancestral_step(self.get_dyn_eta(ss))
        sigma = ss.sigma
        # Calculate the derivative using the model
        d = to_d(x, sigma, ss.denoised)
        dt = sigma_down - sigma

        # Runge-Kutta steps
        k1 = d * dt
        k2 = self.to_d(ss.model(x + k1 / 2, sigma + dt / 2, model_call_idx=1)) * dt
        k3 = self.to_d(ss.model(x + k2 / 2, sigma + dt / 2, model_call_idx=2)) * dt
        k4 = self.to_d(ss.model(x + k3, sigma + dt, model_call_idx=3)) * dt

        # Update the sample
        x = x + (k1 + 2 * k2 + 2 * k3 + k4) / 6
        yield from self.result(ss, x, sigma_up)


# Based on original implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
class EulerDancingStep(SingleStepSampler):
    name = "euler_dancing"
    self_noise = 1

    def __init__(
        self,
        *,
        deta=1.0,
        ds_noise=None,
        leap=2,
        dyn_deta_start=None,
        dyn_deta_end=None,
        dyn_deta_mode="lerp",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.deta = deta
        self.ds_noise = ds_noise if ds_noise is not None else self.s_noise
        self.leap = leap
        self.dyn_deta_start = dyn_deta_start
        self.dyn_deta_end = dyn_deta_end
        if dyn_deta_mode not in ("lerp", "lerp_alt", "deta"):
            raise ValueError("Bad dyn_deta_mode")
        self.dyn_deta_mode = dyn_deta_mode

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        eta = self.eta
        deta = self.deta
        leap_sigmas = ss.sigmas[ss.idx :]
        leap_sigmas = leap_sigmas[: find_first_unsorted(leap_sigmas)]
        zero_idx = (leap_sigmas <= 0).nonzero().flatten()[:1]
        max_leap = (zero_idx.item() if len(zero_idx) else len(leap_sigmas)) - 1
        is_danceable = max_leap > 1 and ss.sigma_next != 0
        curr_leap = max(1, min(self.leap, max_leap))
        sigma_leap = leap_sigmas[curr_leap] if is_danceable else ss.sigma_next
        del leap_sigmas
        sigma_down, sigma_up = get_ancestral_step(ss.sigma, sigma_leap, eta)
        print("???", sigma_down, sigma_up)
        d = to_d(x, ss.sigma, ss.denoised)
        # Euler method
        dt = sigma_down - ss.sigma
        x = x + d * dt
        if curr_leap == 1:
            return (yield from self.result(ss, x, sigma_up))
        noise_strength = self.ds_noise * sigma_up
        if noise_strength != 0:
            x = yield from self.result(
                ss, x, sigma_up, sigma_next=sigma_leap, final=False
            )

            # x = x + self.noise_sampler(ss.sigma, sigma_leap).mul_(
            #     self.ds_noise * sigma_up
            # )
        # sigma_down2, sigma_up2 = get_ancestral_step(sigma_leap, ss.sigma, eta=deta)
        # _sigma_down2, sigma_up2 = get_ancestral_step(sigma_leap, ss.sigma, eta=deta)
        # sigma_up2 = ss.sigma_next + (ss.sigma - ss.sigma_next) * 0.5
        sigma_up2 = get_ancestral_step(ss.sigma_next, sigma_leap, eta=deta)[1] + (
            ss.sigma_next * 0.5
        )
        sigma_down2, _sigma_up2 = get_ancestral_step(
            ss.sigma_next, sigma_leap, eta=deta
        )
        print(">>>", sigma_down2, sigma_up2, "--", ss.sigma, "->", sigma_leap)
        # sigma_down2, sigma_up2 = get_ancestral_step(ss.sigma_next, sigma_leap, eta=deta)
        d_2 = to_d(x, sigma_leap, ss.denoised)
        dt_2 = sigma_down2 - sigma_leap
        x = x + d_2 * dt_2
        yield from self.result(ss, x, sigma_up2)

    def _step(self, x, ss):
        eta = self.get_dyn_eta(ss)
        leap_sigmas = ss.sigmas[ss.idx :]
        leap_sigmas = leap_sigmas[: find_first_unsorted(leap_sigmas)]
        zero_idx = (leap_sigmas <= 0).nonzero().flatten()[:1]
        max_leap = (zero_idx.item() if len(zero_idx) else len(leap_sigmas)) - 1
        is_danceable = max_leap > 1 and ss.sigma_next != 0
        curr_leap = max(1, min(self.leap, max_leap))
        sigma_leap = leap_sigmas[curr_leap] if is_danceable else ss.sigma_next
        # DANCE 35 6 tensor(10.0947, device='cuda:0') -- tensor([21.9220,
        # print("DANCE", max_leap, curr_leap, sigma_leap, "--", leap_sigmas)
        del leap_sigmas
        sigma_down, sigma_up = get_ancestral_step(ss.sigma, sigma_leap, eta)
        d = to_d(x, ss.sigma, ss.denoised)
        # Euler method
        dt = sigma_down - ss.sigma
        x = x + d * dt
        if curr_leap == 1:
            return x, sigma_up
        dance_scale = self.get_dyn_value(ss, self.dyn_deta_start, self.dyn_deta_end)
        if curr_leap == 1 or not is_danceable or abs(dance_scale) < 1e-04:
            print("NODANCE", dance_scale, self.deta, is_danceable, ss.sigma_next)
            yield SamplerResult(ss, self, x, sigma_up)
        print(
            "DANCE", dance_scale, self.deta, self.dyn_deta_mode, self.ds_noise, sigma_up
        )
        sigma_down_normal, sigma_up_normal = get_ancestral_step(
            ss.sigma, ss.sigma_next, eta
        )
        if self.dyn_deta_mode == "lerp":
            dt_normal = sigma_down_normal - ss.sigma
            x_normal = x + d * dt_normal
        else:
            x_normal = x
        sigma_down2, sigma_up2 = get_ancestral_step(
            sigma_leap,
            ss.sigma_next,
            eta=self.deta * (1.0 if self.dyn_deta_mode != "deta" else dance_scale),
        )
        print(
            "-->",
            sigma_down2,
            sigma_up2,
            "--",
            self.deta * (1.0 if self.dyn_deta_mode != "deta" else dance_scale),
        )
        x = x + self.noise_sampler(ss.sigma, sigma_leap).mul_(self.ds_noise * sigma_up)
        d_2 = to_d(x, sigma_leap, ss.denoised)
        dt_2 = sigma_down2 - sigma_leap
        result = x + d_2 * dt_2
        # SIGMA: norm_up=9.062416076660156, up=10.703859329223633, up2=19.376544952392578, str=21.955078125
        noise_strength = sigma_up2 + ((sigma_up - sigma_up_normal) ** 5.0)
        noise_strength = sigma_up2 + ((sigma_up2 - sigma_up) * 0.5)
        # noise_strength = sigma_up2 + (
        #     (sigma_up2 - sigma_up) ** (1.0 - (sigma_up_normal / sigma_up2))
        # )
        noise_diff = (
            sigma_up - sigma_up_normal
            if sigma_up > sigma_up_normal
            else sigma_up_normal - sigma_up
        )
        noise_div = (
            sigma_up / sigma_up_normal
            if sigma_up > sigma_up_normal
            else sigma_up_normal / sigma_up
        )
        noise_diff = sigma_up2 - sigma_up_normal
        noise_div = sigma_up2 / sigma_up_normal
        noise_div = ss.sigma / sigma_leap

        # noise_strength = sigma_up2 + (noise_diff * noise_div)
        # noise_strength = sigma_up2 + ((noise_diff * 0.5) ** 2.0)
        # noise_strength = sigma_up2 + ((1.0 - noise_diff) ** 0.5)
        # noise_strength = sigma_up2 + (((sigma_up2 - sigma_up) * 0.5) ** 2.0)
        # noise_strength = sigma_up2 + (((sigma_up2 - sigma_up_normal) * 0.5) ** 1.5)
        # noise_strength = sigma_up2 + (
        #     (noise_diff * 0.1875) ** (1.0 / (noise_div - 0.0))
        # )
        # noise_strength = sigma_up2 + (
        #     (noise_diff * 0.125) ** (1.0 / (noise_div * 1.25))
        # )
        # noise_strength = sigma_up2 + ((noise_diff * 0.2) ** (1.0 / (noise_div * 1.0)))
        noise_strength = sigma_up2 + (noise_diff * 0.9 * max(0.0, noise_div - 0.8))
        noise_strength = sigma_up2 + (
            (noise_diff / (curr_leap * 0.4))
            * ((noise_div - (curr_leap / 2.0)).clamp(min=0, max=1.5) * 1.0)
        )
        # (1.0 / (noise_div * 1.25)))
        # noise_strength = sigma_up2 + ((noise_diff * 0.5) ** noise_div)
        print(
            f"SIGMA: norm_up={sigma_up_normal}, up={sigma_up}, up2={sigma_up2}, str={noise_strength}",
            # noise_diff,
            noise_div,
        )
        return result, noise_strength

        noise_diff = sigma_up2 - sigma_up * dance_scale
        noise_scale = sigma_up2 + noise_diff * (0.025 * curr_leap)
        # noise_scale = sigma_up2 * self.ds_noise
        if self.dyn_deta_mode == "deta" or dance_scale == 1.0:
            return result, noise_scale
        result = torch.lerp(x_normal, result, dance_scale)
        # FIXME: Broken for noise samplers that care about s/sn
        return result, noise_scale

    # def step(self, x, ss):
    #     eta = self.get_dyn_eta(ss)
    #     leap_sigmas = ss.sigmas[ss.idx :]
    #     leap_sigmas = leap_sigmas[: find_first_unsorted(leap_sigmas)]
    #     zero_idx = (leap_sigmas <= 0).nonzero().flatten()[:1]
    #     max_leap = (zero_idx.item() if len(zero_idx) else len(leap_sigmas)) - 1
    #     is_danceable = max_leap > 1 and ss.sigma_next != 0
    #     curr_leap = max(1, min(self.leap, max_leap))
    #     sigma_leap = leap_sigmas[curr_leap] if is_danceable else ss.sigma_next
    #     # print("DANCE", max_leap, curr_leap, sigma_leap, "--", leap_sigmas)
    #     del leap_sigmas
    #     sigma_down, sigma_up = get_ancestral_step(ss.sigma, sigma_leap, eta)
    #     d = to_d(x, ss.sigma, ss.denoised)
    #     # Euler method
    #     dt = sigma_down - ss.sigma
    #     x = x + d * dt
    #     if curr_leap == 1:
    #         return x, sigma_up
    #     dance_scale = self.get_dyn_value(ss, self.dyn_deta_start, self.dyn_deta_end)
    #     if not is_danceable or abs(dance_scale) < 1e-04:
    #         print("NODANCE", dance_scale, self.deta)
    #         return x, sigma_up
    #     print("NODANCE", dance_scale, self.deta)
    #     sigma_down_normal, _sigma_up_normal = get_ancestral_step(
    #         ss.sigma, ss.sigma_next, eta
    #     )
    #     if self.dyn_deta_mode == "lerp":
    #         dt_normal = sigma_down_normal - ss.sigma
    #         x_normal = x + d * dt_normal
    #     else:
    #         x_normal = x
    #     x = x + self.noise_sampler(ss.sigma, sigma_leap).mul_(self.s_noise * sigma_up)
    #     sigma_down2, sigma_up2 = get_ancestral_step(
    #         sigma_leap,
    #         ss.sigma_next,
    #         eta=self.deta * (1.0 if self.dyn_deta_mode != "deta" else dance_scale),
    #     )
    #     d_2 = to_d(x, sigma_leap, ss.denoised)
    #     dt_2 = sigma_down2 - sigma_leap
    #     result = x + d_2 * dt_2
    #     noise_diff = sigma_up2 - sigma_up * dance_scale
    #     noise_scale = sigma_up2 + noise_diff * (0.025 * curr_leap)
    #     if self.dyn_deta_mode == "deta" or dance_scale == 1.0:
    #         return result, noise_scale
    #     result = torch.lerp(x_normal, result, dance_scale)
    #     # FIXME: Broken for noise samplers that care about s/sn
    #     return result, noise_scale


class DPMPP2SStep(SingleStepSampler, DPMPPStepMixin):
    name = "dpmpp_2s"
    model_calls = 1

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        t_fn, sigma_fn = self.t_fn, self.sigma_fn
        sigma_down, sigma_up = ss.get_ancestral_step(self.get_dyn_eta(ss))
        # DPM-Solver++(2S)
        t, t_next = t_fn(ss.sigma), t_fn(sigma_down)
        r = 1 / 2
        h = t_next - t
        s = t + r * h
        x_2 = (sigma_fn(s) / sigma_fn(t)) * x - (-h * r).expm1() * ss.denoised
        denoised_2 = ss.model(x_2, sigma_fn(s), model_call_idx=1).denoised
        x = (sigma_fn(t_next) / sigma_fn(t)) * x - (-h).expm1() * denoised_2
        yield from self.result(ss, x, sigma_up)


class DPMPPSDEStep(SingleStepSampler, DPMPPStepMixin):
    name = "dpmpp_sde"
    self_noise = 1
    model_calls = 1

    def __init__(self, *args, r=1 / 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.r = r

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        t_fn, sigma_fn = self.t_fn, self.sigma_fn
        r, eta = self.r, self.get_dyn_eta(ss)
        # DPM-Solver++
        t, t_next = t_fn(ss.sigma), t_fn(ss.sigma_next)
        h = t_next - t
        s = t + h * r
        fac = 1 / (2 * r)

        # Step 1
        sd, su = get_ancestral_step(sigma_fn(t), sigma_fn(s), eta)
        s_ = t_fn(sd)
        x_2 = (sigma_fn(s_) / sigma_fn(t)) * x - (t - s_).expm1() * ss.denoised
        x_2 = yield from self.result(
            ss, x_2, su, sigma=sigma_fn(t), sigma_next=sigma_fn(s), final=False
        )
        denoised_2 = ss.model(x_2, sigma_fn(s), model_call_idx=1).denoised

        # Step 2
        sd, su = get_ancestral_step(sigma_fn(t), sigma_fn(t_next), eta)
        t_next_ = t_fn(sd)
        denoised_d = (1 - fac) * ss.denoised + fac * denoised_2
        x = (sigma_fn(t_next_) / sigma_fn(t)) * x - (t - t_next_).expm1() * denoised_d
        yield from self.result(ss, x, su)


# Based on implementation from https://github.com/Clybius/ComfyUI-Extra-Samplers
# Which was originally written by Katherine Crowson
class TTMJVPStep(SingleStepSampler):
    name = "ttm_jvp"
    model_calls = 1

    def __init__(self, *args, alternate_phi_2_calc=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.alternate_phi_2_calc = alternate_phi_2_calc

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.denoised_result(ss))
        eta = self.get_dyn_eta(ss)
        sigma, sigma_next = ss.sigma, ss.sigma_next
        # 2nd order truncated Taylor method
        t, s = -sigma.log(), -sigma_next.log()
        h = s - t
        h_eta = h * (eta + 1)

        eps = to_d(x, sigma, ss.denoised)
        denoised_prime = ss.model(
            x, sigma, tangents=(eps * -sigma, -sigma), model_call_idx=1
        ).jdenoised

        phi_1 = -torch.expm1(-h_eta)
        if self.alternate_phi_2_calc:
            phi_2 = torch.expm1(-h) + h  # seems to work better with eta > 0
        else:
            phi_2 = torch.expm1(-h_eta) + h_eta
        x = torch.exp(-h_eta) * x + phi_1 * ss.denoised + phi_2 * denoised_prime

        noise_scale = (
            sigma_next * torch.sqrt(-torch.expm1(-2 * h * eta))
            if eta
            else ss.sigma.new_zeros(1)
        )
        yield from self.result(ss, x, noise_scale)


# Adapted from https://github.com/zju-pi/diff-sampler/blob/main/diff-solvers-main/solvers.py
# under Apache 2 license
class IPNDMStep(HistorySingleStepSampler):
    name = "ipndm"
    default_history_limit, max_history = 1, 3
    allow_cfgpp = True

    IPNDM_MULTIPLIERS = (
        ((1,), 1),
        ((3, -1), 2),
        ((23, -16, 5), 12),
        ((55, -59, 37, -9), 24),
    )

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        order = self.available_history(ss) + 1
        if order > 1:
            hd = tuple(self.to_d(ss.hist[-hidx]) for hidx in range(order, 1, -1))
        (dm, *hms), div = self.IPNDM_MULTIPLIERS[order - 1]
        noise = dm * self.to_d(ss.hcur)
        for hidx, hm in enumerate(hms, start=1):
            noise += hm * hd[-hidx]
        noise /= div
        yield from self.ancestralize_result(ss, x + ss.dt * noise)


# Adapted from https://github.com/zju-pi/diff-sampler/blob/main/diff-solvers-main/solvers.py
# under Apache 2 license
class IPNDMVStep(HistorySingleStepSampler):
    name = "ipndm_v"
    default_history_limit, max_history = 1, 3
    allow_cfgpp = True

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        dt = ss.dt
        d = self.to_d(ss.hcur)
        order = self.available_history(ss) + 1
        if order > 1:
            hd = tuple(self.to_d(ss.hist[-hidx]) for hidx in range(order, 1, -1))
            hns = (
                ss.sigmas[ss.idx - (order - 2) : ss.idx + 1]
                - ss.sigmas[ss.idx - (order - 1) : ss.idx]
            )
        if order == 1:
            noise = d
        elif order == 2:
            coeff1 = (2 + (dt / hns[-1])) / 2
            coeff2 = -(dt / hns[-1]) / 2
            noise = coeff1 * d + coeff2 * hd[-1]
        elif order == 3:
            temp = (
                1
                - dt
                / (3 * (dt + hns[-1]))
                * (dt * (dt + hns[-1]))
                / (hns[-1] * (hns[-1] + hns[-2]))
            ) / 2
            coeff1 = (2 + (dt / hns[-1])) / 2 + temp
            coeff2 = -(dt / hns[-1]) / 2 - (1 + hns[-1] / hns[-2]) * temp
            coeff3 = temp * hns[-1] / hns[-2]
            noise = coeff1 * d + coeff2 * hd[-1] + coeff3 * hd[-2]
        else:
            temp1 = (
                1
                - dt
                / (3 * (dt + hns[-1]))
                * (dt * (dt + hns[-1]))
                / (hns[-1] * (hns[-1] + hns[-2]))
            ) / 2
            temp2 = (
                (
                    (1 - dt / (3 * (dt + hns[-1]))) / 2
                    + (1 - dt / (2 * (dt + hns[-1])))
                    * dt
                    / (6 * (dt + hns[-1] + hns[-2]))
                )
                * (dt * (dt + hns[-1]) * (dt + hns[-1] + hns[-2]))
                / (hns[-1] * (hns[-1] + hns[-2]) * (hns[-1] + hns[-2] + hns[-3]))
            )
            coeff1 = (2 + (dt / hns[-1])) / 2 + temp1 + temp2
            coeff2 = (
                -(dt / hns[-1]) / 2
                - (1 + hns[-1] / hns[-2]) * temp1
                - (
                    1
                    + (hns[-1] / hns[-2])
                    + (hns[-1] * (hns[-1] + hns[-2]) / (hns[-2] * (hns[-2] + hns[-3])))
                )
                * temp2
            )
            coeff3 = (
                temp1 * hns[-1] / hns[-2]
                + (
                    (hns[-1] / hns[-2])
                    + (hns[-1] * (hns[-1] + hns[-2]) / (hns[-2] * (hns[-2] + hns[-3])))
                    * (1 + hns[-2] / hns[-3])
                )
                * temp2
            )
            coeff4 = (
                -temp2
                * (hns[-1] * (hns[-1] + hns[-2]) / (hns[-2] * (hns[-2] + hns[-3])))
                * hns[-1]
                / hns[-2]
            )
            noise = coeff1 * d + coeff2 * hd[-1] + coeff3 * hd[-2] + coeff4 * hd[-3]
        yield from self.ancestralize_result(ss, x + ss.dt * noise)


class DEISStep(HistorySingleStepSampler):
    name = "deis"
    default_history_limit, max_history = 1, 3
    allow_cfgpp = True

    def __init__(self, *args, deis_mode="tab", **kwargs):
        super().__init__(*args, **kwargs)
        self.deis_mode = deis_mode
        self.deis_coeffs_key = None
        self.deis_coeffs = None

    def get_deis_coeffs(self, ss):
        key = (
            self.history_limit,
            len(ss.sigmas),
            ss.sigmas[0].item(),
            ss.sigmas[-1].item(),
        )
        if self.deis_coeffs_key == key:
            return self.deis_coeffs
        self.deis_coeffs_key = key
        self.deis_coeffs = comfy.k_diffusion.deis.get_deis_coeff_list(
            ss.sigmas, self.history_limit + 1, deis_mode=self.deis_mode
        )
        return self.deis_coeffs

    def step(self, x, ss):
        if ss.sigma_next == 0:
            return (yield from self.euler_step(x, ss))
        dt = ss.dt
        d = self.to_d(ss.hcur)
        order = self.available_history(ss) + 1
        if order < 2:
            noise = dt * d  # Euler
        else:
            c = self.get_deis_coeffs(ss)[ss.idx]
            hd = tuple(self.to_d(ss.hist[-hidx]) for hidx in range(order, 1, -1))
            noise = c[0] * d
            for i in range(1, order):
                noise += c[i] * hd[-i]
        yield from self.ancestralize_result(ss, x + noise)


class HeunPP2Step(SingleStepSampler):
    name = "heunpp2"
    model_calls = 2
    allow_cfgpp = True

    def __init__(self, *args, max_order=3, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_order = max(1, min(self.model_calls + 1, max_order))

    def step(self, x, ss):
        steps_remain = max(0, len(ss.sigmas) - (ss.idx + 2))
        order = min(self.max_order, steps_remain + 1)
        sn = ss.sigma_next
        if order == 1 or sn == 0:
            return (yield from self.euler_step(x, ss))
        d = self.to_d(ss.hcur)
        dt = ss.dt
        w = order * ss.sigma
        w2 = sn / w
        x_2 = x + d * dt
        d_2 = self.to_d(ss.model(x_2, sn, model_call_idx=1))
        if order == 2:
            # Heun's method (ish)
            w1 = 1 - w2
            d_prime = d * w1 + d_2 * w2
        else:
            # Heun++ (ish)
            snn = ss.sigmas[ss.idx + 2]
            dt_2 = snn - sn
            x_3 = x_2 + d_2 * dt_2
            d_3 = self.to_d(ss.model(x_3, snn, model_call_idx=2))
            w3 = snn / w
            w1 = 1 - w2 - w3
            d_prime = w1 * d + w2 * d_2 + w3 * d_3
        yield from self.ancestralize_result(ss, x + d_prime * dt)


class TDEStep(SingleStepSampler, MinSigmaStepMixin):
    name = "tde"
    model_calls = 2
    allow_cfgpp = True

    def __init__(
        self,
        *args,
        ode_solver="rk4",
        ode_max_nfe=100,
        ode_rtol=-2.5,
        ode_atol=-3.5,
        ode_fixup_hack=0.025,
        ode_split=1,
        ode_min_sigma=0.0292,
        **kwargs,
    ):
        if not HAVE_TDE:
            raise RuntimeError(
                "TDE sampler requires torchdiffeq installed in venv. Example: pip install torchdiffeq"
            )
        super().__init__(*args, **kwargs)
        self.ode_solver_name = ode_solver
        self.ode_max_nfe = ode_max_nfe
        self.ode_rtol = 10**ode_rtol
        self.ode_atol = 10**ode_atol
        self.ode_fixup_hack = ode_fixup_hack
        self.ode_split = ode_split
        self.ode_min_sigma = ode_min_sigma if ode_min_sigma is not None else 0.0

    def step(self, x, ss):
        eta = self.get_dyn_eta(ss)
        s, sn = ss.sigma, ss.sigma_next
        if s <= self.ode_min_sigma:
            return (yield from self.euler_step(x, ss))
        sn = self.adjust_step(sn, self.ode_min_sigma)
        sigma_down, sigma_up = ss.get_ancestral_step(eta, sigma_next=sn)
        if self.ode_fixup_hack != 0:
            sigma_down = (sigma_down - (s - sigma_down) * self.ode_fixup_hack).clamp(
                min=0
            )
        delta = (s - sigma_down).item()
        mcc = 0
        bidx = 0
        pbar = None

        def odefn(t, y):
            nonlocal mcc
            if t < 1e-05:
                return torch.zeros_like(y)
            if mcc >= self.ode_max_nfe:
                raise RuntimeError("TDEStep: Model call limit exceeded")

            pct = (s - t) / delta
            pbar.n = round(pct.item() * 999)
            pbar.update(0)
            pbar.set_description(
                f"{self.ode_solver_name}({mcc}/{self.ode_max_nfe})", refresh=True
            )

            if t == ss.sigma and torch.equal(x[bidx], y):
                mr = ss.hcur
                mcc = 1
            else:
                mr = ss.model(y.unsqueeze(0), t, model_call_idx=mcc, s_in=t.new_ones(1))
                mcc += 1
            return self.to_d(
                mr,
                x=y,
                sigma=t,
                denoised=mr.denoised[bidx],
                denoised_uncond=mr.denoised_uncond[bidx],
            )

        result = torch.zeros_like(x)
        t = sigma_down.new_zeros(2)
        torch.linspace(ss.sigma, sigma_down, self.ode_split + 1, out=t)

        for batch in tqdm.trange(
            1,
            x.shape[0] + 1,
            desc="batch",
            leave=True,
            disable=x.shape[0] == 1 or ss.disable_status,
        ):
            bidx = batch - 1
            mcc = 0
            if pbar is not None:
                pbar.close()
            pbar = tqdm.tqdm(
                total=1000,
                desc=self.ode_solver_name,
                leave=True,
                disable=ss.disable_status,
            )
            solution = tde.odeint(
                odefn,
                x[bidx],
                t,
                rtol=self.ode_rtol,
                atol=self.ode_atol,
                method=self.ode_solver_name,
                options={
                    "min_step": 1e-05,
                    "dtype": torch.float64,
                },
            )[-1]
            result[bidx] = solution

        sigma_up, result = yield from self.adjusted_step(ss, sn, result, mcc, sigma_up)
        if pbar is not None:
            pbar.n = pbar.total
            pbar.update(0)
            pbar.close()
        yield from self.result(ss, result, sigma_up)


class TODEStep(SingleStepSampler, MinSigmaStepMixin):
    name = "tode"
    model_calls = 2
    allow_cfgpp = True

    def __init__(
        self,
        *args,
        ode_solver="dopri5",
        ode_max_nfe=100,
        ode_rtol=-1.5,
        ode_atol=-3.5,
        ode_fixup_hack=0.025,
        ode_initial_step=0.25,
        ode_min_sigma=0.0292,
        ode_compile=False,
        ode_ctl_pcoeff=0.3,
        ode_ctl_icoeff=0.9,
        ode_ctl_dcoeff=0.2,
        **kwargs,
    ):
        if not HAVE_TODE:
            raise RuntimeError(
                "TODE sampler requires torchode installed in venv. Example: pip install torchode"
            )
        super().__init__(*args, **kwargs)
        self.ode_solver_name = ode_solver
        self.ode_solver_method = tode.interface.METHODS[ode_solver]
        self.ode_max_nfe = ode_max_nfe
        self.ode_rtol = 10**ode_rtol
        self.ode_atol = 10**ode_atol
        self.ode_ctl_pcoeff = ode_ctl_pcoeff
        self.ode_ctl_icoeff = ode_ctl_icoeff
        self.ode_ctl_dcoeff = ode_ctl_dcoeff
        self.ode_fixup_hack = ode_fixup_hack
        self.ode_compile = ode_compile
        self.ode_min_sigma = ode_min_sigma if ode_min_sigma is not None else 0.0
        self.ode_initial_step = ode_initial_step

    def step(self, x, ss):
        eta = self.get_dyn_eta(ss)
        s, sn = ss.sigma, ss.sigma_next
        if s <= self.ode_min_sigma:
            return (yield from self.euler_step(x, ss))
        sn = self.adjust_step(sn, self.ode_min_sigma)
        sigma_down, sigma_up = ss.get_ancestral_step(eta, sigma_next=sn)
        if self.ode_fixup_hack != 0:
            sigma_down = (sigma_down - (s - sigma_down) * self.ode_fixup_hack).clamp(
                min=0
            )

        delta = (ss.sigma - sigma_down).item()
        mcc = 0
        pbar = None
        b, c, h, w = x.shape

        def odefn(t, y_flat):
            nonlocal mcc
            if torch.all(t <= 1e-05).item():
                return torch.zeros_like(y_flat)
            if mcc >= self.ode_max_nfe:
                raise RuntimeError("TDEStep: Model call limit exceeded")

            pct = (s - t) / delta
            pbar.n = round(pct.min().item() * 999)
            pbar.update(0)
            pbar.set_description(
                f"{self.ode_solver_name}({mcc}/{self.ode_max_nfe})", refresh=True
            )
            y = y_flat.reshape(-1, c, h, w)
            t32 = t.to(torch.float32)
            del y_flat

            if mcc == 0 and torch.all(t == s):
                mr = ss.hcur
                mcc = 1
            else:
                mr = ss.model(y, t32.clamp(min=1e-05), model_call_idx=mcc)
                mcc += 1
            result = self.to_d(mr).flatten(start_dim=1)
            for bi in range(t.shape[0]):
                if t[bi] <= 1e-05:
                    result[bi, :] = 0
            return result

        result = torch.zeros_like(x)
        t = torch.stack((s, sigma_down)).to(torch.float64).repeat(b, 1)

        pbar = tqdm.tqdm(
            total=1000, desc=self.ode_solver_name, leave=True, disable=ss.disable_status
        )

        term = tode.ODETerm(odefn)
        method = self.ode_solver_method(term=term)
        controller = tode.PIDController(
            term=term,
            atol=self.ode_atol,
            rtol=self.ode_rtol,
            dt_min=1e-05,
            pcoeff=self.ode_ctl_pcoeff,
            icoeff=self.ode_ctl_icoeff,
            dcoeff=self.ode_ctl_dcoeff,
        )
        solver_ = tode.AutoDiffAdjoint(method, controller)
        solver = solver_ if not self.ode_compile else torch.compile(solver_)
        problem = tode.InitialValueProblem(
            y0=x.flatten(start_dim=1), t_start=t[:, 0], t_end=t[:, -1]
        )
        dt0 = (
            (t[:, -1] - t[:, 0]) * self.ode_initial_step
            if self.ode_initial_step
            else None
        )
        solution = solver.solve(problem, dt0=dt0)

        # print("\nSOLUTION", solution.stats, solution.ys.shape)
        result = solution.ys[:, -1].reshape(-1, c, h, w)
        del solution

        sigma_up, result = yield from self.adjusted_step(ss, sn, result, mcc, sigma_up)
        if pbar is not None:
            pbar.n = pbar.total
            pbar.update(0)
            pbar.close()
        yield from self.result(ss, result, sigma_up)


STEP_SAMPLERS = {
    "default (euler)": EulerStep,
    "bogacki (2)": BogackiStep,
    "deis": DEISStep,
    "dpmpp_2m_sde": DPMPP2MSDEStep,
    "dpmpp_2m": DPMPP2MStep,
    "dpmpp_2s": DPMPP2SStep,
    "dpmpp_3m_sde": DPMPP3MSDEStep,
    "dpmpp_sde (1)": DPMPPSDEStep,
    "euler_cycle": EulerCycleStep,
    "euler_dancing": EulerDancingStep,
    "euler": EulerStep,
    "heunpp (1-2)": HeunPP2Step,
    "ipndm_v": IPNDMVStep,
    "ipndm": IPNDMStep,
    "res (1)": RESStep,
    "reversible_bogacki (2)": ReversibleBogackiStep,
    "reversible_heun (1)": ReversibleHeunStep,
    "reversible_heun_1s": ReversibleHeun1SStep,
    "rk4 (3)": RK4Step,
    "tde (variable)": TDEStep,
    "tode (variable)": TODEStep,
    "trapezoidal (1)": TrapezoidalStep,
    "trapezoidal_cycle (1)": TrapezoidalCycleStep,
    "ttm_jvp (1)": TTMJVPStep,
}

__all__ = (
    "STEP_SAMPLERS",
    "EulerStep",
    "EulerCycleStep",
    "DPMPP2MStep",
    "DPMPP2MSDEStep",
    "DPMPP3MSDEStep",
    "DPMPP2SStep",
    "ReversibleHeunStep",
    "ReversibleHeun1SStep",
    "RESStep",
    "TrapezoidalCycleStep",
    "TrapezoidalStep",
    "BogackiStep",
    "ReversibleBogackiStep",
    "EulerDancingStep",
    "TTMJVPStep",
    "IPNDMStep",
    "IPNDMVStep",
    "TDEStep",
)
