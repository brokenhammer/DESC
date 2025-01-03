"""Objectives for targeting neoclassical transport."""

import numpy as np
from orthax.legendre import leggauss

from desc.compute import get_profiles, get_transforms
from desc.compute.utils import _compute as compute_fun
from desc.grid import LinearGrid
from desc.utils import Timer

from ..integrals.quad_utils import (
    automorphism_sin,
    chebgauss2,
    get_quadrature,
    grad_automorphism_sin,
)
from .objective_funs import _Objective, collect_docs
from .utils import _parse_callable_target_bounds

_bounce_overwrite = {
    "deriv_mode": """
    deriv_mode : {"auto", "fwd", "rev"}
        Specify how to compute Jacobian matrix, either forward mode or reverse mode AD.
        ``auto`` selects forward or reverse mode based on the size of the input and
        output of the objective. Has no effect on ``self.grad`` or ``self.hess`` which
        always use reverse mode and forward over reverse mode respectively.

        Default is ``fwd``. If ``rev`` is chosen, then ``jac_chunk_size=1`` is chosen
        by default. In ``rev`` mode, reducing the pitch angle parameter ``batch_size``
        does not reduce memory, so it is recommended to retain the default for that.
        """
}


class EffectiveRipple(_Objective):
    """The effective ripple is a proxy for neoclassical transport.

    The 3D geometry of the magnetic field in stellarators produces local magnetic
    wells that lead to bad confinement properties with enhanced radial drift,
    especially for trapped particles. Neoclassical (thermal) transport can become the
    dominant transport channel in stellarators which are not optimized to reduce it.
    The effective ripple is a proxy, measuring the effective modulation amplitude of the
    magnetic field averaged along a magnetic surface, which can be used to optimize for
    stellarators with improved confinement. It is targeted as a flux surface function.

    References
    ----------
    https://doi.org/10.1063/1.873749.
    Evaluation of 1/ν neoclassical transport in stellarators.
    V. V. Nemov, S. V. Kasilov, W. Kernbichler, M. F. Heyn.
    Phys. Plasmas 1 December 1999; 6 (12): 4622–4632.

    Parameters
    ----------
    eq : Equilibrium
        ``Equilibrium`` to be optimized.
    rho : ndarray
        Unique coordinate values specifying flux surfaces to compute on.
    alpha : ndarray
        Unique coordinate values specifying field line labels to compute on.
    batch : bool
        Whether to vectorize part of the computation. Default is true.
    Y_B : int
        Desired resolution for algorithm to compute bounce points.
        Default is double ``Y``. Something like 100 is usually sufficient.
        Currently, this is the number of knots per toroidal transit over
        to approximate |B| with cubic splines.
    num_transit : int
        Number of toroidal transits to follow field line.
        For axisymmetric devices, one poloidal transit is sufficient. Otherwise,
        assuming the surface is not near rational, more transits will
        approximate surface averages better, with diminishing returns.
    num_well : int
        Maximum number of wells to detect for each pitch and field line.
        Giving ``None`` will detect all wells but due to current limitations in
        JAX this will have worse performance.
        Specifying a number that tightly upper bounds the number of wells will
        increase performance. In general, an upper bound on the number of wells
        per toroidal transit is ``Aι+B`` where ``A``,``B`` are the poloidal and
        toroidal Fourier resolution of |B|, respectively, in straight-field line
        PEST coordinates, and ι is the rotational transform normalized by 2π.
        A tighter upper bound than ``num_well=(Aι+B)*num_transit`` is preferable.
        The ``check_points`` or ``plot`` methods in ``desc.integrals.Bounce2D``
        are useful to select a reasonable value.
    num_quad : int
        Resolution for quadrature of bounce integrals. Default is 32.
    num_pitch : int
        Resolution for quadrature over velocity coordinate. Default is 50.

    """

    __doc__ = __doc__.rstrip() + collect_docs(
        target_default="``target=0``.",
        bounds_default="``target=0``.",
        normalize_detail=" Note: Has no effect for this objective.",
        normalize_target_detail=" Note: Has no effect for this objective.",
        overwrite=_bounce_overwrite,
    )

    _coordinates = "r"
    _units = "~"
    _print_value_fmt = "Effective ripple ε: "

    def __init__(
        self,
        eq,
        *,
        target=None,
        bounds=None,
        weight=1,
        normalize=True,
        normalize_target=True,
        loss_function=None,
        deriv_mode="auto",
        rho=1.0,
        alpha=0.0,
        batch=True,
        Y_B=100,
        num_transit=10,
        num_quad=32,
        num_pitch=50,
        num_well=None,
        name="Effective ripple",
        jac_chunk_size=None,
    ):
        if target is None and bounds is None:
            target = 0.0

        rho, alpha = np.atleast_1d(rho, alpha)
        self._dim_f = rho.size
        self._keys_1dr = [
            "iota",
            "iota_r",
            "<|grad(rho)|>",
            "min_tz |B|",
            "max_tz |B|",
            "R0",  # TODO: GitHub PR #1094
        ]
        self._constants = {
            "quad_weights": 1.0,
            "rho": rho,
            "alpha": alpha,
            "zeta": np.linspace(0, 2 * np.pi * num_transit, Y_B * num_transit),
            "quad": chebgauss2(num_quad),
        }
        self._hyperparameters = {
            "num_pitch": num_pitch,
            "batch": batch,
            "num_well": num_well,
        }

        super().__init__(
            things=eq,
            target=target,
            bounds=bounds,
            weight=weight,
            normalize=normalize,
            normalize_target=normalize_target,
            loss_function=loss_function,
            deriv_mode=deriv_mode,
            name=name,
            jac_chunk_size=jac_chunk_size,
        )

    def build(self, use_jit=True, verbose=1):
        """Build constant arrays.

        Parameters
        ----------
        use_jit : bool, optional
            Whether to just-in-time compile the objective and derivatives.
        verbose : int, optional
            Level of output.

        """
        eq = self.things[0]
        self._grid_1dr = LinearGrid(
            rho=self._constants["rho"], M=eq.M_grid, N=eq.N_grid, NFP=eq.NFP, sym=eq.sym
        )
        self._target, self._bounds = _parse_callable_target_bounds(
            self._target, self._bounds, self._constants["rho"]
        )

        timer = Timer()
        if verbose > 0:
            print("Precomputing transforms")
        timer.start("Precomputing transforms")

        self._constants["transforms_1dr"] = get_transforms(
            self._keys_1dr, eq, self._grid_1dr
        )
        self._constants["profiles"] = get_profiles(
            self._keys_1dr + ["effective ripple"], eq, self._grid_1dr
        )

        timer.stop("Precomputing transforms")
        if verbose > 1:
            timer.disp("Precomputing transforms")

        super().build(use_jit=use_jit, verbose=verbose)

    def compute(self, params, constants=None):
        """Compute the effective ripple.

        Parameters
        ----------
        params : dict
            Dictionary of equilibrium degrees of freedom, e.g.
            ``Equilibrium.params_dict``
        constants : dict
            Dictionary of constant data, e.g. transforms, profiles etc.
            Defaults to ``self.constants``.

        Returns
        -------
        result : ndarray
            Effective ripple as a function of the flux surface label.

        """
        if constants is None:
            constants = self.constants
        eq = self.things[0]
        data = compute_fun(
            eq,
            self._keys_1dr,
            params,
            constants["transforms_1dr"],
            constants["profiles"],
        )
        grid = eq._get_rtz_grid(
            constants["rho"],
            constants["alpha"],
            constants["zeta"],
            coordinates="raz",
            iota=self._grid_1dr.compress(data["iota"]),
            params=params,
        )
        data = {
            key: (
                grid.copy_data_from_other(data[key], self._grid_1dr)
                if key != "R0"
                else data[key]
            )
            for key in self._keys_1dr
        }
        data = compute_fun(
            eq,
            "effective ripple",
            params,
            get_transforms("effective ripple", eq, grid, jitable=True),
            constants["profiles"],
            data=data,
            quad=constants["quad"],
            **self._hyperparameters,
        )
        return grid.compress(data["effective ripple"])


class GammaC(_Objective):
    """Γ_c is a proxy for measuring energetic ion confinement.

    References
    ----------
    Poloidal motion of trapped particle orbits in real-space coordinates.
    V. V. Nemov, S. V. Kasilov, W. Kernbichler, G. O. Leitold.
    Phys. Plasmas 1 May 2008; 15 (5): 052501.
    https://doi.org/10.1063/1.2912456.
    Equation 61.

    A model for the fast evaluation of prompt losses of energetic ions in stellarators.
    J.L. Velasco et al. 2021 Nucl. Fusion 61 116059.
    https://doi.org/10.1088/1741-4326/ac2994.
    Equation 16.

    Parameters
    ----------
    eq : Equilibrium
        ``Equilibrium`` to be optimized.
    rho : ndarray
        Unique coordinate values specifying flux surfaces to compute on.
    alpha : ndarray
        Unique coordinate values specifying field line labels to compute on.
    batch : bool
        Whether to vectorize part of the computation. Default is true.
    Y_B : int
        Desired resolution for algorithm to compute bounce points.
        Default is double ``Y``. Something like 100 is usually sufficient.
        Currently, this is the number of knots per toroidal transit over
        to approximate |B| with cubic splines.
    num_transit : int
        Number of toroidal transits to follow field line.
        For axisymmetric devices, one poloidal transit is sufficient. Otherwise,
        assuming the surface is not near rational, more transits will
        approximate surface averages better, with diminishing returns.
    num_well : int
        Maximum number of wells to detect for each pitch and field line.
        Giving ``None`` will detect all wells but due to current limitations in
        JAX this will have worse performance.
        Specifying a number that tightly upper bounds the number of wells will
        increase performance. In general, an upper bound on the number of wells
        per toroidal transit is ``Aι+B`` where ``A``,``B`` are the poloidal and
        toroidal Fourier resolution of |B|, respectively, in straight-field line
        PEST coordinates, and ι is the rotational transform normalized by 2π.
        A tighter upper bound than ``num_well=(Aι+B)*num_transit`` is preferable.
        The ``check_points`` or ``plot`` methods in ``desc.integrals.Bounce2D``
        are useful to select a reasonable value.
    num_quad : int
        Resolution for quadrature of bounce integrals. Default is 32.
    num_pitch : int
        Resolution for quadrature over velocity coordinate. Default is 64.
    Nemov : bool
        Whether to use the Γ_c as defined by Nemov et al. or Velasco et al.
        Default is Nemov. Set to ``False`` to use Velascos's.

        Nemov's Γ_c converges to a finite nonzero value in the infinity limit
        of the number of toroidal transits. Velasco's expression has a secular
        term that drives the result to zero as the number of toroidal transits
        increases if the secular term is not averaged out from the singular
        integrals. Currently, an optimization using Velasco's metric may need
        to be evaluated by measuring decrease in Γ_c at a fixed number of toroidal
        transits.

    """

    __doc__ = __doc__.rstrip() + collect_docs(
        target_default="``target=0``.",
        bounds_default="``target=0``.",
        normalize_detail=" Note: Has no effect for this objective.",
        normalize_target_detail=" Note: Has no effect for this objective.",
        overwrite=_bounce_overwrite,
    )

    _coordinates = "r"
    _units = "~"
    _print_value_fmt = "Γ_c: "

    def __init__(
        self,
        eq,
        *,
        target=None,
        bounds=None,
        weight=1,
        normalize=True,
        normalize_target=True,
        loss_function=None,
        deriv_mode="auto",
        rho=np.linspace(0.5, 1, 3),
        alpha=np.array([0]),
        batch=True,
        num_transit=10,
        Y_B=100,
        num_quad=32,
        num_pitch=64,
        num_well=None,
        Nemov=True,
        name="Gamma_c",
        jac_chunk_size=None,
    ):
        if target is None and bounds is None:
            target = 0.0

        rho, alpha = np.atleast_1d(rho, alpha)
        self._dim_f = rho.size
        self._constants = {
            "quad_weights": 1.0,
            "rho": rho,
            "alpha": alpha,
            "zeta": np.linspace(0, 2 * np.pi * num_transit, Y_B * num_transit),
        }
        self._hyperparameters = {
            "num_quad": num_quad,
            "num_pitch": num_pitch,
            "batch": batch,
            "num_well": num_well,
        }
        self._keys_1dr = ["iota", "iota_r", "min_tz |B|", "max_tz |B|"]
        self._key = "Gamma_c" if Nemov else "Gamma_c Velasco"

        super().__init__(
            things=eq,
            target=target,
            bounds=bounds,
            weight=weight,
            normalize=normalize,
            normalize_target=normalize_target,
            loss_function=loss_function,
            deriv_mode=deriv_mode,
            name=name,
            jac_chunk_size=jac_chunk_size,
        )

    def build(self, use_jit=True, verbose=1):
        """Build constant arrays.

        Parameters
        ----------
        use_jit : bool, optional
            Whether to just-in-time compile the objective and derivatives.
        verbose : int, optional
            Level of output.

        """
        eq = self.things[0]
        self._grid_1dr = LinearGrid(
            rho=self._constants["rho"], M=eq.M_grid, N=eq.N_grid, NFP=eq.NFP, sym=eq.sym
        )
        num_quad = self._hyperparameters.pop("num_quad")
        self._constants["quad"] = get_quadrature(
            leggauss(num_quad),
            (automorphism_sin, grad_automorphism_sin),
        )
        if self._key == "Gamma_c":
            self._constants["quad2"] = chebgauss2(num_quad)
        self._target, self._bounds = _parse_callable_target_bounds(
            self._target, self._bounds, self._constants["rho"]
        )

        timer = Timer()
        if verbose > 0:
            print("Precomputing transforms")
        timer.start("Precomputing transforms")

        self._constants["transforms_1dr"] = get_transforms(
            self._keys_1dr, eq, self._grid_1dr
        )
        self._constants["profiles"] = get_profiles(
            self._keys_1dr + [self._key], eq, self._grid_1dr
        )

        timer.stop("Precomputing transforms")
        if verbose > 1:
            timer.disp("Precomputing transforms")

        super().build(use_jit=use_jit, verbose=verbose)

    def compute(self, params, constants=None):
        """Compute Γ_c.

        Parameters
        ----------
        params : dict
            Dictionary of equilibrium degrees of freedom, e.g.
            ``Equilibrium.params_dict``
        constants : dict
            Dictionary of constant data, e.g. transforms, profiles etc.
            Defaults to ``self.constants``.

        Returns
        -------
        result : ndarray
            Γ_c as a function of the flux surface label.

        """
        if constants is None:
            constants = self.constants
        eq = self.things[0]
        data = compute_fun(
            eq,
            self._keys_1dr,
            params,
            constants["transforms_1dr"],
            constants["profiles"],
        )
        grid = eq._get_rtz_grid(
            constants["rho"],
            constants["alpha"],
            constants["zeta"],
            coordinates="raz",
            iota=self._grid_1dr.compress(data["iota"]),
            params=params,
        )
        data = {
            key: grid.copy_data_from_other(data[key], self._grid_1dr)
            for key in self._keys_1dr
        }
        quad2 = {}
        if self._key == "Gamma_c":
            quad2["quad2"] = constants["quad2"]
        data = compute_fun(
            eq,
            self._key,
            params,
            get_transforms(self._key, eq, grid, jitable=True),
            constants["profiles"],
            data=data,
            quad=constants["quad"],
            **quad2,
            **self._hyperparameters,
        )
        return grid.compress(data[self._key])
