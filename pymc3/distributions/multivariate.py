#   Copyright 2020 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

#!/usr/bin/env python
# -*- coding: utf-8 -*-

import warnings

import aesara
import aesara.tensor as at
import numpy as np
import scipy

from aesara.graph.basic import Apply
from aesara.graph.op import Op
from aesara.tensor import gammaln
from aesara.tensor.nlinalg import det, eigh, matrix_inverse, trace
from aesara.tensor.random.basic import MultinomialRV, dirichlet, multivariate_normal
from aesara.tensor.random.op import RandomVariable, default_shape_from_params
from aesara.tensor.random.utils import broadcast_params
from aesara.tensor.slinalg import (
    Cholesky,
    Solve,
    solve_lower_triangular,
    solve_upper_triangular,
)
from aesara.tensor.type import TensorType
from scipy import linalg, stats

import pymc3 as pm

from pymc3.aesaraf import floatX, intX
from pymc3.distributions import transforms
from pymc3.distributions.continuous import ChiSquared, Normal, assert_negative_support
from pymc3.distributions.dist_math import bound, factln, logpow, multigammaln
from pymc3.distributions.distribution import Continuous, Discrete
from pymc3.math import kron_diag, kron_dot, kron_solve_lower, kronecker, sigmoid

__all__ = [
    "MvNormal",
    "MvStudentT",
    "Dirichlet",
    "Multinomial",
    "DirichletMultinomial",
    "OrderedMultinomial",
    "Wishart",
    "WishartBartlett",
    "LKJCorr",
    "LKJCholeskyCov",
    "MatrixNormal",
    "KroneckerNormal",
    "CAR",
]

solve_lower = Solve(A_structure="lower_triangular")
# Step methods and advi do not catch LinAlgErrors at the
# moment. We work around that by using a cholesky op
# that returns a nan as first entry instead of raising
# an error.
cholesky = Cholesky(lower=True, on_error="nan")


def quaddist_matrix(cov=None, chol=None, tau=None, lower=True, *args, **kwargs):
    if chol is not None and not lower:
        chol = chol.T

    if len([i for i in [tau, cov, chol] if i is not None]) != 1:
        raise ValueError("Incompatible parameterization. Specify exactly one of tau, cov, or chol.")

    if cov is not None:
        cov = at.as_tensor_variable(cov)
        if cov.ndim != 2:
            raise ValueError("cov must be two dimensional.")
    elif tau is not None:
        tau = at.as_tensor_variable(tau)
        if tau.ndim != 2:
            raise ValueError("tau must be two dimensional.")
        # TODO: What's the correct order/approach (in the non-square case)?
        # `aesara.tensor.nlinalg.tensorinv`?
        cov = matrix_inverse(tau)
    else:
        # TODO: What's the correct order/approach (in the non-square case)?
        chol = at.as_tensor_variable(chol)
        if chol.ndim != 2:
            raise ValueError("chol must be two dimensional.")
        cov = chol.dot(chol.T)

    return cov


def quaddist_parse(value, mu, cov, mat_type="cov"):
    """Compute (x - mu).T @ Sigma^-1 @ (x - mu) and the logdet of Sigma."""
    if value.ndim > 2 or value.ndim == 0:
        raise ValueError("Invalid dimension for value: %s" % value.ndim)
    if value.ndim == 1:
        onedim = True
        value = value[None, :]
    else:
        onedim = False

    delta = value - mu
    # Use this when Theano#5908 is released.
    # return MvNormalLogp()(self.cov, delta)
    chol_cov = cholesky(cov)
    if mat_type == "cov" or mat_type != "tau":
        dist, logdet, ok = quaddist_chol(delta, chol_cov)
    else:
        dist, logdet, ok = quaddist_tau(delta, chol_cov)
    if onedim:
        return dist[0], logdet, ok

    return dist, logdet, ok


def quaddist_chol(delta, chol_mat):
    diag = at.diag(chol_mat)
    # Check if the covariance matrix is positive definite.
    ok = at.all(diag > 0)
    # If not, replace the diagonal. We return -inf later, but
    # need to prevent solve_lower from throwing an exception.
    chol_cov = at.switch(ok, chol_mat, 1)

    delta_trans = solve_lower(chol_cov, delta.T).T
    quaddist = (delta_trans ** 2).sum(axis=-1)
    logdet = at.sum(at.log(diag))
    return quaddist, logdet, ok


def quaddist_tau(delta, chol_mat):
    diag = at.nlinalg.diag(chol_mat)
    # Check if the precision matrix is positive definite.
    ok = at.all(diag > 0)
    # If not, replace the diagonal. We return -inf later, but
    # need to prevent solve_lower from throwing an exception.
    chol_tau = at.switch(ok, chol_mat, 1)

    delta_trans = at.dot(delta, chol_tau)
    quaddist = (delta_trans ** 2).sum(axis=-1)
    logdet = -at.sum(at.log(diag))
    return quaddist, logdet, ok


class MvNormal(Continuous):
    r"""
    Multivariate normal log-likelihood.

    .. math::

       f(x \mid \pi, T) =
           \frac{|T|^{1/2}}{(2\pi)^{k/2}}
           \exp\left\{ -\frac{1}{2} (x-\mu)^{\prime} T (x-\mu) \right\}

    ========  ==========================
    Support   :math:`x \in \mathbb{R}^k`
    Mean      :math:`\mu`
    Variance  :math:`T^{-1}`
    ========  ==========================

    Parameters
    ----------
    mu: array
        Vector of means.
    cov: array
        Covariance matrix. Exactly one of cov, tau, or chol is needed.
    tau: array
        Precision matrix. Exactly one of cov, tau, or chol is needed.
    chol: array
        Cholesky decomposition of covariance matrix. Exactly one of cov,
        tau, or chol is needed.
    lower: bool, default=True
        Whether chol is the lower tridiagonal cholesky factor.

    Examples
    --------
    Define a multivariate normal variable for a given covariance
    matrix::

        cov = np.array([[1., 0.5], [0.5, 2]])
        mu = np.zeros(2)
        vals = pm.MvNormal('vals', mu=mu, cov=cov, shape=(5, 2))

    Most of the time it is preferable to specify the cholesky
    factor of the covariance instead. For example, we could
    fit a multivariate outcome like this (see the docstring
    of `LKJCholeskyCov` for more information about this)::

        mu = np.zeros(3)
        true_cov = np.array([[1.0, 0.5, 0.1],
                             [0.5, 2.0, 0.2],
                             [0.1, 0.2, 1.0]])
        data = np.random.multivariate_normal(mu, true_cov, 10)

        sd_dist = pm.Exponential.dist(1.0, shape=3)
        chol, corr, stds = pm.LKJCholeskyCov('chol_cov', n=3, eta=2,
            sd_dist=sd_dist, compute_corr=True)
        vals = pm.MvNormal('vals', mu=mu, chol=chol, observed=data)

    For unobserved values it can be better to use a non-centered
    parametrization::

        sd_dist = pm.Exponential.dist(1.0, shape=3)
        chol, _, _ = pm.LKJCholeskyCov('chol_cov', n=3, eta=2,
            sd_dist=sd_dist, compute_corr=True)
        vals_raw = pm.Normal('vals_raw', mu=0, sigma=1, shape=(5, 3))
        vals = pm.Deterministic('vals', at.dot(chol, vals_raw.T).T)
    """
    rv_op = multivariate_normal

    @classmethod
    def dist(cls, mu, cov=None, tau=None, chol=None, lower=True, **kwargs):
        mu = at.as_tensor_variable(mu)
        cov = quaddist_matrix(cov, chol, tau, lower)
        return super().dist([mu, cov], **kwargs)

    def logp(value, mu, cov):
        """
        Calculate log-probability of Multivariate Normal distribution
        at specified value.

        Parameters
        ----------
        value: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        quaddist, logdet, ok = quaddist_parse(value, mu, cov)
        k = floatX(value.shape[-1])
        norm = -0.5 * k * pm.floatX(np.log(2 * np.pi))
        return bound(norm - 0.5 * quaddist - logdet, ok)

    def _distr_parameters_for_repr(self):
        return ["mu", "cov"]


class MvStudentTRV(RandomVariable):
    name = "multivariate_studentt"
    ndim_supp = 1
    ndims_params = [0, 1, 2]
    dtype = "floatX"
    _print_name = ("MvStudentT", "\\operatorname{MvStudentT}")

    def __call__(self, nu, mu=None, cov=None, size=None, **kwargs):

        dtype = aesara.config.floatX if self.dtype == "floatX" else self.dtype

        if mu is None:
            mu = np.array([0.0], dtype=dtype)
        if cov is None:
            cov = np.array([[1.0]], dtype=dtype)
        return super().__call__(nu, mu, cov, size=size, **kwargs)

    def _shape_from_params(self, dist_params, rep_param_idx=1, param_shapes=None):
        return default_shape_from_params(self.ndim_supp, dist_params, rep_param_idx, param_shapes)

    @classmethod
    def rng_fn(cls, rng, nu, mu, cov, size):

        # Don't reassign broadcasted cov, since MvNormal expects two dimensional cov only.
        mu, _ = broadcast_params([mu, cov], cls.ndims_params[1:])

        chi2_samples = np.sqrt(rng.chisquare(nu, size=size) / nu)
        # Add distribution shape to chi2 samples
        chi2_samples = chi2_samples.reshape(chi2_samples.shape + (1,) * len(mu.shape))

        mv_samples = multivariate_normal.rng_fn(rng=rng, mean=np.zeros_like(mu), cov=cov, size=size)

        size = tuple(size or ())
        if size:
            mu = np.broadcast_to(mu, size + mu.shape)

        return (mv_samples / chi2_samples) + mu


mv_studentt = MvStudentTRV()


class MvStudentT(Continuous):
    r"""
    Multivariate Student-T log-likelihood.

    .. math::
        f(\mathbf{x}| \nu,\mu,\Sigma) =
        \frac
            {\Gamma\left[(\nu+p)/2\right]}
            {\Gamma(\nu/2)\nu^{p/2}\pi^{p/2}
             \left|{\Sigma}\right|^{1/2}
             \left[
               1+\frac{1}{\nu}
               ({\mathbf x}-{\mu})^T
               {\Sigma}^{-1}({\mathbf x}-{\mu})
             \right]^{-(\nu+p)/2}}

    ========  =============================================
    Support   :math:`x \in \mathbb{R}^p`
    Mean      :math:`\mu` if :math:`\nu > 1` else undefined
    Variance  :math:`\frac{\nu}{\mu-2}\Sigma`
                  if :math:`\nu>2` else undefined
    ========  =============================================

    Parameters
    ----------
    nu: float
        Degrees of freedom, should be a positive scalar.
    Sigma: matrix
        Covariance matrix. Use `cov` in new code.
    mu: array
        Vector of means.
    cov: matrix
        The covariance matrix.
    tau: matrix
        The precision matrix.
    chol: matrix
        The cholesky factor of the covariance matrix.
    lower: bool, default=True
        Whether the cholesky fatcor is given as a lower triangular matrix.
    """
    rv_op = mv_studentt

    @classmethod
    def dist(cls, nu, Sigma=None, mu=None, cov=None, tau=None, chol=None, lower=True, **kwargs):
        if Sigma is not None:
            if cov is not None:
                raise ValueError("Specify only one of cov and Sigma")
            cov = Sigma
        nu = at.as_tensor_variable(floatX(nu))
        mu = at.as_tensor_variable(floatX(mu))
        cov = quaddist_matrix(cov, chol, tau, lower)
        assert_negative_support(nu, "nu", "MvStudentT")
        return super().dist([nu, mu, cov], **kwargs)

    def logp(value, nu, mu, cov):
        """
        Calculate log-probability of Multivariate Student's T distribution
        at specified value.

        Parameters
        ----------
        value: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        quaddist, logdet, ok = quaddist_parse(value, mu, cov)
        k = floatX(value.shape[-1])

        norm = gammaln((nu + k) / 2.0) - gammaln(nu / 2.0) - 0.5 * k * at.log(nu * np.pi)
        inner = -(nu + k) / 2.0 * at.log1p(quaddist / nu)
        return bound(norm + inner - logdet, ok)

    def _distr_parameters_for_repr(self):
        return ["nu", "mu", "cov"]


class Dirichlet(Continuous):
    r"""
    Dirichlet log-likelihood.

    .. math::

       f(\mathbf{x}|\mathbf{a}) =
           \frac{\Gamma(\sum_{i=1}^k a_i)}{\prod_{i=1}^k \Gamma(a_i)}
           \prod_{i=1}^k x_i^{a_i - 1}

    ========  ===============================================
    Support   :math:`x_i \in (0, 1)` for :math:`i \in \{1, \ldots, K\}`
              such that :math:`\sum x_i = 1`
    Mean      :math:`\dfrac{a_i}{\sum a_i}`
    Variance  :math:`\dfrac{a_i - \sum a_0}{a_0^2 (a_0 + 1)}`
              where :math:`a_0 = \sum a_i`
    ========  ===============================================

    Parameters
    ----------
    a: array
        Concentration parameters (a > 0).
    """

    rv_op = dirichlet

    def __new__(cls, name, *args, **kwargs):
        kwargs.setdefault("transform", transforms.stick_breaking)
        return super().__new__(cls, name, *args, **kwargs)

    @classmethod
    def dist(cls, a, **kwargs):

        a = at.as_tensor_variable(a)
        # mean = a / at.sum(a)
        # mode = at.switch(at.all(a > 1), (a - 1) / at.sum(a - 1), np.nan)

        return super().dist([a], **kwargs)

    def logp(value, a):
        """
        Calculate log-probability of Dirichlet distribution
        at specified value.

        Parameters
        ----------
        value: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        # only defined for sum(value) == 1
        return bound(
            at.sum(logpow(value, a - 1) - gammaln(a), axis=-1) + gammaln(at.sum(a, axis=-1)),
            at.all(value >= 0),
            at.all(value <= 1),
            at.all(a > 0),
            broadcast_conditions=False,
        )

    def _distr_parameters_for_repr(self):
        return ["a"]


class MultinomialRV(MultinomialRV):
    """Aesara's `MultinomialRV` doesn't broadcast; this one does."""

    @classmethod
    def rng_fn(cls, rng, n, p, size):
        if n.ndim > 0 or p.ndim > 1:
            n, p = broadcast_params([n, p], cls.ndims_params)
            size = tuple(size or ())

            if size:
                n = np.broadcast_to(n, size + n.shape)
                p = np.broadcast_to(p, size + p.shape)

            res = np.empty(p.shape)
            for idx in np.ndindex(p.shape[:-1]):
                res[idx] = rng.multinomial(n[idx], p[idx])
            return res
        else:
            return rng.multinomial(n, p, size=size)


multinomial = MultinomialRV()


class Multinomial(Discrete):
    r"""
    Multinomial log-likelihood.

    Generalizes binomial distribution, but instead of each trial resulting
    in "success" or "failure", each one results in exactly one of some
    fixed finite number k of possible outcomes over n independent trials.
    'x[i]' indicates the number of times outcome number i was observed
    over the n trials.

    .. math::

       f(x \mid n, p) = \frac{n!}{\prod_{i=1}^k x_i!} \prod_{i=1}^k p_i^{x_i}

    ==========  ===========================================
    Support     :math:`x \in \{0, 1, \ldots, n\}` such that
                :math:`\sum x_i = n`
    Mean        :math:`n p_i`
    Variance    :math:`n p_i (1 - p_i)`
    Covariance  :math:`-n p_i p_j` for :math:`i \ne j`
    ==========  ===========================================

    Parameters
    ----------
    n: int or array
        Number of trials (n > 0). If n is an array its shape must be (N,) with
        N = p.shape[0]
    p: one- or two-dimensional array
        Probability of each one of the different outcomes. Elements must
        be non-negative and sum to 1 along the last axis. They will be
        automatically rescaled otherwise.
    """
    rv_op = multinomial

    @classmethod
    def dist(cls, n, p, *args, **kwargs):

        p = p / at.sum(p, axis=-1, keepdims=True)
        n = at.as_tensor_variable(n)
        p = at.as_tensor_variable(p)

        # mean = n * p
        # mode = at.cast(at.round(mean), "int32")
        # diff = n - at.sum(mode, axis=-1, keepdims=True)
        # inc_bool_arr = at.abs_(diff) > 0
        # mode = at.inc_subtensor(mode[inc_bool_arr.nonzero()], diff[inc_bool_arr.nonzero()])
        return super().dist([n, p], *args, **kwargs)

    def logp(value, n, p):
        """
        Calculate log-probability of Multinomial distribution
        at specified value.

        Parameters
        ----------
        x: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        return bound(
            factln(n) + at.sum(-factln(value) + logpow(p, value), axis=-1),
            at.all(value >= 0),
            at.all(at.eq(at.sum(value, axis=-1), n)),
            at.all(p <= 1),
            at.all(at.eq(at.sum(p, axis=-1), 1)),
            at.all(at.ge(n, 0)),
            broadcast_conditions=False,
        )


class DirichletMultinomial(Discrete):
    r"""Dirichlet Multinomial log-likelihood.

    Dirichlet mixture of Multinomials distribution, with a marginalized PMF.

    .. math::

        f(x \mid n, a) = \frac{\Gamma(n + 1)\Gamma(\sum a_k)}
                              {\Gamma(\n + \sum a_k)}
                         \prod_{k=1}^K
                         \frac{\Gamma(x_k +  a_k)}
                              {\Gamma(x_k + 1)\Gamma(a_k)}

    ==========  ===========================================
    Support     :math:`x \in \{0, 1, \ldots, n\}` such that
                :math:`\sum x_i = n`
    Mean        :math:`n \frac{a_i}{\sum{a_k}}`
    ==========  ===========================================

    Parameters
    ----------
    n : int or array
        Total counts in each replicate. If n is an array its shape must be (N,)
        with N = a.shape[0]

    a : one- or two-dimensional array
        Dirichlet parameter. Elements must be strictly positive.
        The number of categories is given by the length of the last axis.

    shape : integer tuple
        Describes shape of distribution. For example if n=array([5, 10]), and
        a=array([1, 1, 1]), shape should be (2, 3).
    """

    def __init__(self, n, a, shape, *args, **kwargs):

        super().__init__(shape=shape, defaults=("_defaultval",), *args, **kwargs)

        n = intX(n)
        a = floatX(a)
        if len(self.shape) > 1:
            self.n = at.shape_padright(n)
            self.a = at.as_tensor_variable(a) if a.ndim > 1 else at.shape_padleft(a)
        else:
            # n is a scalar, p is a 1d array
            self.n = at.as_tensor_variable(n)
            self.a = at.as_tensor_variable(a)

        p = self.a / self.a.sum(-1, keepdims=True)

        self.mean = self.n * p
        # Mode is only an approximation. Exact computation requires a complex
        # iterative algorithm as described in https://doi.org/10.1016/j.spl.2009.09.013
        mode = at.cast(at.round(self.mean), "int32")
        diff = self.n - at.sum(mode, axis=-1, keepdims=True)
        inc_bool_arr = at.abs_(diff) > 0
        mode = at.inc_subtensor(mode[inc_bool_arr.nonzero()], diff[inc_bool_arr.nonzero()])
        self._defaultval = mode

    def _random(self, n, a, size=None):
        # numpy will cast dirichlet and multinomial samples to float64 by default
        original_dtype = a.dtype

        # Thanks to the default shape handling done in generate_values, the last
        # axis of n is a dummy axis that allows it to broadcast well with `a`
        n = np.broadcast_to(n, size)
        a = np.broadcast_to(a, size)
        n = n[..., 0]

        # np.random.multinomial needs `n` to be a scalar int and `a` a
        # sequence so we semi flatten them and iterate over them
        n_ = n.reshape([-1])
        a_ = a.reshape([-1, a.shape[-1]])
        p_ = np.array([np.random.dirichlet(aa) for aa in a_])
        samples = np.array([np.random.multinomial(nn, pp) for nn, pp in zip(n_, p_)])
        samples = samples.reshape(a.shape)

        # We cast back to the original dtype
        return samples.astype(original_dtype)

    def random(self, point=None, size=None):
        """
        Draw random values from Dirichlet-Multinomial distribution.

        Parameters
        ----------
        point: dict, optional
            Dict of variable values on which random values are to be
            conditioned (uses default point if not specified).
        size: int, optional
            Desired size of random sample (returns one sample if not
            specified).

        Returns
        -------
        array
        """
        # n, a = draw_values([self.n, self.a], point=point, size=size)
        # samples = generate_samples(
        #     self._random,
        #     n,
        #     a,
        #     dist_shape=self.shape,
        #     size=size,
        # )
        #
        # # If distribution is initialized with .dist(), valid init shape is not asserted.
        # # Under normal use in a model context valid init shape is asserted at start.
        # expected_shape = to_tuple(size) + to_tuple(self.shape)
        # sample_shape = tuple(samples.shape)
        # if sample_shape != expected_shape:
        #     raise ShapeError(
        #         f"Expected sample shape was {expected_shape} but got {sample_shape}. "
        #         "This may reflect an invalid initialization shape."
        #     )
        #
        # return samples

    def logp(self, value):
        """
        Calculate log-probability of DirichletMultinomial distribution
        at specified value.

        Parameters
        ----------
        value: integer array
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        a = self.a
        n = self.n
        sum_a = a.sum(axis=-1, keepdims=True)

        const = (gammaln(n + 1) + gammaln(sum_a)) - gammaln(n + sum_a)
        series = gammaln(value + a) - (gammaln(value + 1) + gammaln(a))
        result = const + series.sum(axis=-1, keepdims=True)
        # Bounds checking to confirm parameters and data meet all constraints
        # and that each observation value_i sums to n_i.
        return bound(
            result,
            value >= 0,
            a > 0,
            n >= 0,
            at.eq(value.sum(axis=-1, keepdims=True), n),
            broadcast_conditions=False,
        )

    def _distr_parameters_for_repr(self):
        return ["n", "a"]


class OrderedMultinomial(Multinomial):
    rv_op = multinomial

    @classmethod
    def dist(cls, eta, cutpoints, n, compute_p=True, *args, **kwargs):
        eta = at.as_tensor_variable(floatX(eta))
        cutpoints = at.as_tensor_variable(cutpoints)
        n = at.as_tensor_variable(intX(n))

        pa = sigmoid(cutpoints - at.shape_padright(eta))
        p_cum = at.concatenate(
            [
                at.zeros_like(at.shape_padright(pa[..., 0])),
                pa,
                at.ones_like(at.shape_padright(pa[..., 0])),
            ],
            axis=-1,
        )
        if compute_p and pm.modelcontext(None):
            p = pm.Deterministic("complete_p", p_cum[..., 1:] - p_cum[..., :-1])
        else:
            p = p_cum[..., 1:] - p_cum[..., :-1]

        return super().dist(n, p, *args, **kwargs)


def posdef(AA):
    try:
        linalg.cholesky(AA)
        return 1
    except linalg.LinAlgError:
        return 0


class PosDefMatrix(Op):
    """
    Check if input is positive definite. Input should be a square matrix.

    """

    # Properties attribute
    __props__ = ()

    # Compulsory if itypes and otypes are not defined

    def make_node(self, x):
        x = at.as_tensor_variable(x)
        assert x.ndim == 2
        o = TensorType(dtype="int8", broadcastable=[])()
        return Apply(self, [x], [o])

    # Python implementation:
    def perform(self, node, inputs, outputs):

        (x,) = inputs
        (z,) = outputs
        try:
            z[0] = np.array(posdef(x), dtype="int8")
        except Exception:
            pm._log.exception("Failed to check if %s positive definite", x)
            raise

    def infer_shape(self, fgraph, node, shapes):
        return [[]]

    def grad(self, inp, grads):
        (x,) = inp
        return [x.zeros_like(aesara.config.floatX)]

    def __str__(self):
        return "MatrixIsPositiveDefinite"


matrix_pos_def = PosDefMatrix()


class Wishart(Continuous):
    r"""
    Wishart log-likelihood.

    The Wishart distribution is the probability distribution of the
    maximum-likelihood estimator (MLE) of the precision matrix of a
    multivariate normal distribution.  If V=1, the distribution is
    identical to the chi-square distribution with nu degrees of
    freedom.

    .. math::

       f(X \mid nu, T) =
           \frac{{\mid T \mid}^{nu/2}{\mid X \mid}^{(nu-k-1)/2}}{2^{nu k/2}
           \Gamma_p(nu/2)} \exp\left\{ -\frac{1}{2} Tr(TX) \right\}

    where :math:`k` is the rank of :math:`X`.

    ========  =========================================
    Support   :math:`X(p x p)` positive definite matrix
    Mean      :math:`nu V`
    Variance  :math:`nu (v_{ij}^2 + v_{ii} v_{jj})`
    ========  =========================================

    Parameters
    ----------
    nu: int
        Degrees of freedom, > 0.
    V: array
        p x p positive definite matrix.

    Notes
    -----
    This distribution is unusable in a PyMC3 model. You should instead
    use LKJCholeskyCov or LKJCorr.
    """

    def __init__(self, nu, V, *args, **kwargs):
        super().__init__(*args, **kwargs)
        warnings.warn(
            "The Wishart distribution can currently not be used "
            "for MCMC sampling. The probability of sampling a "
            "symmetric matrix is basically zero. Instead, please "
            "use LKJCholeskyCov or LKJCorr. For more information "
            "on the issues surrounding the Wishart see here: "
            "https://github.com/pymc-devs/pymc3/issues/538.",
            UserWarning,
        )
        self.nu = nu = at.as_tensor_variable(nu)
        self.p = p = at.as_tensor_variable(V.shape[0])
        self.V = V = at.as_tensor_variable(V)
        self.mean = nu * V
        self.mode = at.switch(at.ge(nu, p + 1), (nu - p - 1) * V, np.nan)

    def random(self, point=None, size=None):
        """
        Draw random values from Wishart distribution.

        Parameters
        ----------
        point: dict, optional
            Dict of variable values on which random values are to be
            conditioned (uses default point if not specified).
        size: int, optional
            Desired size of random sample (returns one sample if not
            specified).

        Returns
        -------
        array
        """
        # nu, V = draw_values([self.nu, self.V], point=point, size=size)
        # size = 1 if size is None else size
        # return generate_samples(stats.wishart.rvs, nu.item(), V, broadcast_shape=(size,))

    def logp(self, X):
        """
        Calculate log-probability of Wishart distribution
        at specified value.

        Parameters
        ----------
        X: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        nu = self.nu
        p = self.p
        V = self.V

        IVI = det(V)
        IXI = det(X)

        return bound(
            (
                (nu - p - 1) * at.log(IXI)
                - trace(matrix_inverse(V).dot(X))
                - nu * p * at.log(2)
                - nu * at.log(IVI)
                - 2 * multigammaln(nu / 2.0, p)
            )
            / 2,
            matrix_pos_def(X),
            at.eq(X, X.T),
            nu > (p - 1),
            broadcast_conditions=False,
        )


def WishartBartlett(name, S, nu, is_cholesky=False, return_cholesky=False, initval=None):
    R"""
    Bartlett decomposition of the Wishart distribution. As the Wishart
    distribution requires the matrix to be symmetric positive semi-definite
    it is impossible for MCMC to ever propose acceptable matrices.

    Instead, we can use the Barlett decomposition which samples a lower
    diagonal matrix. Specifically:

    .. math::
        \text{If} L \sim \begin{pmatrix}
        \sqrt{c_1} & 0 & 0 \\
        z_{21} & \sqrt{c_2} & 0 \\
        z_{31} & z_{32} & \sqrt{c_3}
        \end{pmatrix}

        \text{with} c_i \sim \chi^2(n-i+1) \text{ and } n_{ij} \sim \mathcal{N}(0, 1), \text{then} \\
        L \times A \times A.T \times L.T \sim \text{Wishart}(L \times L.T, \nu)

    See http://en.wikipedia.org/wiki/Wishart_distribution#Bartlett_decomposition
    for more information.

    Parameters
    ----------
    S: ndarray
        p x p positive definite matrix
        Or:
        p x p lower-triangular matrix that is the Cholesky factor
        of the covariance matrix.
    nu: int
        Degrees of freedom, > dim(S).
    is_cholesky: bool (default=False)
        Input matrix S is already Cholesky decomposed as S.T * S
    return_cholesky: bool (default=False)
        Only return the Cholesky decomposed matrix.
    initval: ndarray
        p x p positive definite matrix used to initialize

    Notes
    -----
    This is not a standard Distribution class but follows a similar
    interface. Besides the Wishart distribution, it will add RVs
    name_c and name_z to your model which make up the matrix.

    This distribution is usually a bad idea to use as a prior for multivariate
    normal. You should instead use LKJCholeskyCov or LKJCorr.
    """

    L = S if is_cholesky else scipy.linalg.cholesky(S)
    diag_idx = np.diag_indices_from(S)
    tril_idx = np.tril_indices_from(S, k=-1)
    n_diag = len(diag_idx[0])
    n_tril = len(tril_idx[0])

    if initval is not None:
        # Inverse transform
        initval = np.dot(np.dot(np.linalg.inv(L), initval), np.linalg.inv(L.T))
        initval = linalg.cholesky(initval, lower=True)
        diag_testval = initval[diag_idx] ** 2
        tril_testval = initval[tril_idx]
    else:
        diag_testval = None
        tril_testval = None

    c = at.sqrt(
        ChiSquared("%s_c" % name, nu - np.arange(2, 2 + n_diag), shape=n_diag, initval=diag_testval)
    )
    pm._log.info("Added new variable %s_c to model diagonal of Wishart." % name)
    z = Normal("%s_z" % name, 0.0, 1.0, shape=n_tril, initval=tril_testval)
    pm._log.info("Added new variable %s_z to model off-diagonals of Wishart." % name)
    # Construct A matrix
    A = at.zeros(S.shape, dtype=np.float32)
    A = at.set_subtensor(A[diag_idx], c)
    A = at.set_subtensor(A[tril_idx], z)

    # L * A * A.T * L.T ~ Wishart(L*L.T, nu)
    if return_cholesky:
        return pm.Deterministic(name, at.dot(L, A))
    else:
        return pm.Deterministic(name, at.dot(at.dot(at.dot(L, A), A.T), L.T))


def _lkj_normalizing_constant(eta, n):
    if eta == 1:
        result = gammaln(2.0 * at.arange(1, int((n - 1) / 2) + 1)).sum()
        if n % 2 == 1:
            result += (
                0.25 * (n ** 2 - 1) * at.log(np.pi)
                - 0.25 * (n - 1) ** 2 * at.log(2.0)
                - (n - 1) * gammaln(int((n + 1) / 2))
            )
        else:
            result += (
                0.25 * n * (n - 2) * at.log(np.pi)
                + 0.25 * (3 * n ** 2 - 4 * n) * at.log(2.0)
                + n * gammaln(n / 2)
                - (n - 1) * gammaln(n)
            )
    else:
        result = -(n - 1) * gammaln(eta + 0.5 * (n - 1))
        k = at.arange(1, n)
        result += (0.5 * k * at.log(np.pi) + gammaln(eta + 0.5 * (n - 1 - k))).sum()
    return result


class _LKJCholeskyCov(Continuous):
    r"""Underlying class for covariance matrix with LKJ distributed correlations.
    See docs for LKJCholeskyCov function for more details on how to use it in models.
    """

    def __init__(self, eta, n, sd_dist, *args, **kwargs):
        self.n = at.as_tensor_variable(n)
        self.eta = at.as_tensor_variable(eta)

        if "transform" in kwargs and kwargs["transform"] is not None:
            raise ValueError("Invalid parameter: transform.")
        if "shape" in kwargs:
            raise ValueError("Invalid parameter: shape.")

        shape = n * (n + 1) // 2

        if sd_dist.shape.ndim not in [0, 1]:
            raise ValueError("Invalid shape for sd_dist.")

        def transform_params(rv_var):
            _, _, _, n, eta = rv_var.owner.inputs
            return np.arange(1, n + 1).cumsum() - 1

        transform = transforms.CholeskyCovPacked(transform_params)

        kwargs["shape"] = shape
        kwargs["transform"] = transform
        super().__init__(*args, **kwargs)

        self.sd_dist = sd_dist
        self.diag_idxs = transform.diag_idxs

        self.mode = floatX(np.zeros(shape))
        self.mode[self.diag_idxs] = 1

    def logp(self, x):
        """
        Calculate log-probability of Covariance matrix with LKJ
        distributed correlations at specified value.

        Parameters
        ----------
        x: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        n = self.n
        eta = self.eta

        diag_idxs = self.diag_idxs
        cumsum = at.cumsum(x ** 2)
        variance = at.zeros(n)
        variance = at.inc_subtensor(variance[0], x[0] ** 2)
        variance = at.inc_subtensor(variance[1:], cumsum[diag_idxs[1:]] - cumsum[diag_idxs[:-1]])
        sd_vals = at.sqrt(variance)

        logp_sd = self.sd_dist.logp(sd_vals).sum()
        corr_diag = x[diag_idxs] / sd_vals

        logp_lkj = (2 * eta - 3 + n - at.arange(n)) * at.log(corr_diag)
        logp_lkj = at.sum(logp_lkj)

        # Compute the log det jacobian of the second transformation
        # described in the docstring.
        idx = at.arange(n)
        det_invjac = at.log(corr_diag) - idx * at.log(sd_vals)
        det_invjac = det_invjac.sum()

        norm = _lkj_normalizing_constant(eta, n)

        return norm + logp_lkj + logp_sd + det_invjac

    def _random(self, n, eta, size=1):
        eta_sample_shape = (size,) + eta.shape
        P = np.eye(n) * np.ones(eta_sample_shape + (n, n))
        # original implementation in R see:
        # https://github.com/rmcelreath/rethinking/blob/master/R/distributions.r
        beta = eta - 1.0 + n / 2.0
        r12 = 2.0 * stats.beta.rvs(a=beta, b=beta, size=eta_sample_shape) - 1.0
        P[..., 0, 1] = r12
        P[..., 1, 1] = np.sqrt(1.0 - r12 ** 2)
        for mp1 in range(2, n):
            beta -= 0.5
            y = stats.beta.rvs(a=mp1 / 2.0, b=beta, size=eta_sample_shape)
            z = stats.norm.rvs(loc=0, scale=1, size=eta_sample_shape + (mp1,))
            z = z / np.sqrt(np.einsum("ij,ij->j", z, z))
            P[..., 0:mp1, mp1] = np.sqrt(y[..., np.newaxis]) * z
            P[..., mp1, mp1] = np.sqrt(1.0 - y)
        C = np.einsum("...ji,...jk->...ik", P, P)
        D = np.atleast_1d(self.sd_dist.random(size=P.shape[:-2]))
        if D.shape in [tuple(), (1,)]:
            D = self.sd_dist.random(size=P.shape[:-1])
        elif D.ndim < C.ndim - 1:
            D = [D] + [self.sd_dist.random(size=P.shape[:-2]) for _ in range(n - 1)]
            D = np.moveaxis(np.array(D), 0, C.ndim - 2)
        elif D.ndim == C.ndim - 1:
            if D.shape[-1] == 1:
                D = [D] + [self.sd_dist.random(size=P.shape[:-2]) for _ in range(n - 1)]
                D = np.concatenate(D, axis=-1)
            elif D.shape[-1] != n:
                raise ValueError(
                    "The size of the samples drawn from the "
                    "supplied sd_dist.random have the wrong "
                    "size. Expected {} but got {} instead.".format(n, D.shape[-1])
                )
        else:
            raise ValueError(
                "Supplied sd_dist.random generates samples with "
                "too many dimensions. It must yield samples "
                "with 0 or 1 dimensions. Got {} instead".format(D.ndim - C.ndim - 2)
            )
        C *= D[..., :, np.newaxis] * D[..., np.newaxis, :]
        tril_idx = np.tril_indices(n, k=0)
        return np.linalg.cholesky(C)[..., tril_idx[0], tril_idx[1]]

    def random(self, point=None, size=None):
        """
        Draw random values from Covariance matrix with LKJ
        distributed correlations.

        Parameters
        ----------
        point: dict, optional
            Dict of variable values on which random values are to be
            conditioned (uses default point if not specified).
        size: int, optional
            Desired size of random sample (returns one sample if not
            specified).

        Returns
        -------
        array
        """
        # # Get parameters and broadcast them
        # n, eta = draw_values([self.n, self.eta], point=point, size=size)
        # broadcast_shape = np.broadcast(n, eta).shape
        # # We can only handle cov matrices with a constant n per random call
        # n = np.unique(n)
        # if len(n) > 1:
        #     raise RuntimeError("Varying n is not supported for LKJCholeskyCov")
        # n = int(n[0])
        # dist_shape = ((n * (n + 1)) // 2,)
        # # We make sure that eta and the drawn n get their shapes broadcasted
        # eta = np.broadcast_to(eta, broadcast_shape)
        # # We change the size of the draw depending on the broadcast shape
        # sample_shape = broadcast_shape + dist_shape
        # if size is not None:
        #     if not isinstance(size, tuple):
        #         try:
        #             size = tuple(size)
        #         except TypeError:
        #             size = (size,)
        #     if size == sample_shape:
        #         size = None
        #     elif size == broadcast_shape:
        #         size = None
        #     elif size[-len(sample_shape) :] == sample_shape:
        #         size = size[: len(size) - len(sample_shape)]
        #     elif size[-len(broadcast_shape) :] == broadcast_shape:
        #         size = size[: len(size) - len(broadcast_shape)]
        # # We will always provide _random with an integer size and then reshape
        # # the output to get the correct size
        # if size is not None:
        #     _size = np.prod(size)
        # else:
        #     _size = 1
        # samples = self._random(n, eta, size=_size)
        # if size is None:
        #     samples = samples[0]
        # else:
        #     samples = np.reshape(samples, size + sample_shape)
        # return samples

    def _distr_parameters_for_repr(self):
        return ["eta", "n"]


def LKJCholeskyCov(name, eta, n, sd_dist, compute_corr=False, store_in_trace=True, *args, **kwargs):
    r"""Wrapper function for covariance matrix with LKJ distributed correlations.

    This defines a distribution over Cholesky decomposed covariance
    matrices, such that the underlying correlation matrices follow an
    LKJ distribution [1] and the standard deviations follow an arbitray
    distribution specified by the user.

    Parameters
    ----------
    name: str
        The name given to the variable in the model.
    eta: float
        The shape parameter (eta > 0) of the LKJ distribution. eta = 1
        implies a uniform distribution of the correlation matrices;
        larger values put more weight on matrices with few correlations.
    n: int
        Dimension of the covariance matrix (n > 1).
    sd_dist: pm.Distribution
        A distribution for the standard deviations.
    compute_corr: bool, default=False
        If `True`, returns three values: the Cholesky decomposition, the correlations
        and the standard deviations of the covariance matrix. Otherwise, only returns
        the packed Cholesky decomposition. Defaults to `False` to ensure backwards
        compatibility.
    store_in_trace: bool, default=True
        Whether to store the correlations and standard deviations of the covariance
        matrix in the posterior trace. If `True`, they will automatically be named as
        `{name}_corr` and `{name}_stds` respectively. Effective only when
        `compute_corr=True`.

    Returns
    -------
    packed_chol: TensorVariable
        If `compute_corr=False` (default). The packed Cholesky covariance decomposition.
    chol:  TensorVariable
        If `compute_corr=True`. The unpacked Cholesky covariance decomposition.
    corr: TensorVariable
        If `compute_corr=True`. The correlations of the covariance matrix.
    stds: TensorVariable
        If `compute_corr=True`. The standard deviations of the covariance matrix.

    Notes
    -----
    Since the Cholesky factor is a lower triangular matrix, we use packed storage for
    the matrix: We store the values of the lower triangular matrix in a one-dimensional
    array, numbered by row::

        [[0 - - -]
         [1 2 - -]
         [3 4 5 -]
         [6 7 8 9]]

    The unpacked Cholesky covariance matrix is automatically computed and returned when
    you specify `compute_corr=True` in `pm.LKJCholeskyCov` (see example below).
    Otherwise, you can use `pm.expand_packed_triangular(packed_cov, lower=True)`
    to convert the packed Cholesky matrix to a regular two-dimensional array.

    Examples
    --------
    .. code:: python

        with pm.Model() as model:
            # Note that we access the distribution for the standard
            # deviations, and do not create a new random variable.
            sd_dist = pm.Exponential.dist(1.0)
            chol, corr, sigmas = pm.LKJCholeskyCov('chol_cov', eta=4, n=10,
            sd_dist=sd_dist, compute_corr=True)

            # if you only want the packed Cholesky (default behavior):
            # packed_chol = pm.LKJCholeskyCov('chol_cov', eta=4, n=10, sd_dist=sd_dist)
            # chol = pm.expand_packed_triangular(10, packed_chol, lower=True)

            # Define a new MvNormal with the given covariance
            vals = pm.MvNormal('vals', mu=np.zeros(10), chol=chol, shape=10)

            # Or transform an uncorrelated normal:
            vals_raw = pm.Normal('vals_raw', mu=0, sigma=1, shape=10)
            vals = at.dot(chol, vals_raw)

            # Or compute the covariance matrix
            cov = at.dot(chol, chol.T)

    **Implementation** In the unconstrained space all values of the cholesky factor
    are stored untransformed, except for the diagonal entries, where
    we use a log-transform to restrict them to positive values.

    To correctly compute log-likelihoods for the standard deviations
    and the correlation matrix seperatly, we need to consider a
    second transformation: Given a cholesky factorization
    :math:`LL^T = \Sigma` of a covariance matrix we can recover the
    standard deviations :math:`\sigma` as the euclidean lengths of
    the rows of :math:`L`, and the cholesky factor of the
    correlation matrix as :math:`U = \text{diag}(\sigma)^{-1}L`.
    Since each row of :math:`U` has length 1, we do not need to
    store the diagonal. We define a transformation :math:`\phi`
    such that :math:`\phi(L)` is the lower triangular matrix containing
    the standard deviations :math:`\sigma` on the diagonal and the
    correlation matrix :math:`U` below. In this form we can easily
    compute the different likelihoods separately, as the likelihood
    of the correlation matrix only depends on the values below the
    diagonal, and the likelihood of the standard deviation depends
    only on the diagonal values.

    We still need the determinant of the jacobian of :math:`\phi^{-1}`.
    If we think of :math:`\phi` as an automorphism on
    :math:`\mathbb{R}^{\tfrac{n(n+1)}{2}}`, where we order
    the dimensions as described in the notes above, the jacobian
    is a block-diagonal matrix, where each block corresponds to
    one row of :math:`U`. Each block has arrowhead shape, and we
    can compute the determinant of that as described in [2]. Since
    the determinant of a block-diagonal matrix is the product
    of the determinants of the blocks, we get

    .. math::

       \text{det}(J_{\phi^{-1}}(U)) =
       \left[
         \prod_{i=2}^N u_{ii}^{i - 1} L_{ii}
       \right]^{-1}

    References
    ----------
    .. [1] Lewandowski, D., Kurowicka, D. and Joe, H. (2009).
       "Generating random correlation matrices based on vines and
       extended onion method." Journal of multivariate analysis,
       100(9), pp.1989-2001.

    .. [2] J. M. isn't a mathematician (http://math.stackexchange.com/users/498/
       j-m-isnt-a-mathematician), Different approaches to evaluate this
       determinant, URL (version: 2012-04-14):
       http://math.stackexchange.com/q/130026
    """
    # compute Cholesky decomposition
    packed_chol = _LKJCholeskyCov(name, eta=eta, n=n, sd_dist=sd_dist)
    if not compute_corr:
        return packed_chol

    else:
        chol = pm.expand_packed_triangular(n, packed_chol, lower=True)
        # compute covariance matrix
        cov = at.dot(chol, chol.T)
        # extract standard deviations and rho
        stds = at.sqrt(at.diag(cov))
        inv_stds = 1 / stds
        corr = inv_stds[None, :] * cov * inv_stds[:, None]
        if store_in_trace:
            stds = pm.Deterministic(f"{name}_stds", stds)
            corr = pm.Deterministic(f"{name}_corr", corr)

        return chol, corr, stds


class LKJCorr(Continuous):
    r"""
    The LKJ (Lewandowski, Kurowicka and Joe) log-likelihood.

    The LKJ distribution is a prior distribution for correlation matrices.
    If eta = 1 this corresponds to the uniform distribution over correlation
    matrices. For eta -> oo the LKJ prior approaches the identity matrix.

    ========  ==============================================
    Support   Upper triangular matrix with values in [-1, 1]
    ========  ==============================================

    Parameters
    ----------
    n: int
        Dimension of the covariance matrix (n > 1).
    eta: float
        The shape parameter (eta > 0) of the LKJ distribution. eta = 1
        implies a uniform distribution of the correlation matrices;
        larger values put more weight on matrices with few correlations.

    Notes
    -----
    This implementation only returns the values of the upper triangular
    matrix excluding the diagonal. Here is a schematic for n = 5, showing
    the indexes of the elements::

        [[- 0 1 2 3]
         [- - 4 5 6]
         [- - - 7 8]
         [- - - - 9]
         [- - - - -]]


    References
    ----------
    .. [LKJ2009] Lewandowski, D., Kurowicka, D. and Joe, H. (2009).
        "Generating random correlation matrices based on vines and
        extended onion method." Journal of multivariate analysis,
        100(9), pp.1989-2001.
    """

    def __init__(self, eta=None, n=None, p=None, transform="interval", *args, **kwargs):
        if (p is not None) and (n is not None) and (eta is None):
            warnings.warn(
                "Parameters to LKJCorr have changed: shape parameter n -> eta "
                "dimension parameter p -> n. Please update your code. "
                "Automatically re-assigning parameters for backwards compatibility.",
                DeprecationWarning,
            )
            self.n = p
            self.eta = n
            eta = self.eta
            n = self.n
        elif (n is not None) and (eta is not None) and (p is None):
            self.n = n
            self.eta = eta
        else:
            raise ValueError(
                "Invalid parameter: please use eta as the shape parameter and "
                "n as the dimension parameter."
            )

        shape = n * (n - 1) // 2
        self.mean = floatX(np.zeros(shape))

        if transform == "interval":
            transform = transforms.interval(-1, 1)

        super().__init__(shape=shape, transform=transform, *args, **kwargs)
        warnings.warn(
            "Parameters in LKJCorr have been rename: shape parameter n -> eta "
            "dimension parameter p -> n. Please double check your initialization.",
            DeprecationWarning,
        )
        self.tri_index = np.zeros([n, n], dtype="int32")
        self.tri_index[np.triu_indices(n, k=1)] = np.arange(shape)
        self.tri_index[np.triu_indices(n, k=1)[::-1]] = np.arange(shape)

    def _random(self, n, eta, size=None):
        size = size if isinstance(size, tuple) else (size,)
        # original implementation in R see:
        # https://github.com/rmcelreath/rethinking/blob/master/R/distributions.r
        beta = eta - 1.0 + n / 2.0
        r12 = 2.0 * stats.beta.rvs(a=beta, b=beta, size=size) - 1.0
        P = np.eye(n)[:, :, np.newaxis] * np.ones(size)
        P[0, 1] = r12
        P[1, 1] = np.sqrt(1.0 - r12 ** 2)
        for mp1 in range(2, n):
            beta -= 0.5
            y = stats.beta.rvs(a=mp1 / 2.0, b=beta, size=size)
            z = stats.norm.rvs(loc=0, scale=1, size=(mp1,) + size)
            z = z / np.sqrt(np.einsum("ij,ij->j", z, z))
            P[0:mp1, mp1] = np.sqrt(y) * z
            P[mp1, mp1] = np.sqrt(1.0 - y)
        C = np.einsum("ji...,jk...->...ik", P, P)
        triu_idx = np.triu_indices(n, k=1)
        return C[..., triu_idx[0], triu_idx[1]]

    def random(self, point=None, size=None):
        """
        Draw random values from LKJ distribution.

        Parameters
        ----------
        point: dict, optional
            Dict of variable values on which random values are to be
            conditioned (uses default point if not specified).
        size: int, optional
            Desired size of random sample (returns one sample if not
            specified).

        Returns
        -------
        array
        """
        # n, eta = draw_values([self.n, self.eta], point=point, size=size)
        # size = 1 if size is None else size
        # samples = generate_samples(self._random, n, eta, broadcast_shape=(size,))
        # return samples

    def logp(self, x):
        """
        Calculate log-probability of LKJ distribution at specified
        value.

        Parameters
        ----------
        x: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        n = self.n
        eta = self.eta

        X = x[self.tri_index]
        X = at.fill_diagonal(X, 1)

        result = _lkj_normalizing_constant(eta, n)
        result += (eta - 1.0) * at.log(det(X))
        return bound(
            result,
            X >= -1,
            X <= 1,
            matrix_pos_def(X),
            eta > 0,
            broadcast_conditions=False,
        )

    def _distr_parameters_for_repr(self):
        return ["eta", "n"]


class MatrixNormal(Continuous):
    r"""
    Matrix-valued normal log-likelihood.

    .. math::
       f(x \mid \mu, U, V) =
           \frac{1}{(2\pi^{m n} |U|^n |V|^m)^{1/2}}
           \exp\left\{
                -\frac{1}{2} \mathrm{Tr}[ V^{-1} (x-\mu)^{\prime} U^{-1} (x-\mu)]
            \right\}

    ===============  =====================================
    Support          :math:`x \in \mathbb{R}^{m \times n}`
    Mean             :math:`\mu`
    Row Variance     :math:`U`
    Column Variance  :math:`V`
    ===============  =====================================

    Parameters
    ----------
    mu: array
        Array of means. Must be broadcastable with the random variable X such
        that the shape of mu + X is (m,n).
    rowcov: mxm array
        Among-row covariance matrix. Defines variance within
        columns. Exactly one of rowcov or rowchol is needed.
    rowchol: mxm array
        Cholesky decomposition of among-row covariance matrix. Exactly one of
        rowcov or rowchol is needed.
    colcov: nxn array
        Among-column covariance matrix. If rowcov is the identity matrix,
        this functions as `cov` in MvNormal.
        Exactly one of colcov or colchol is needed.
    colchol: nxn array
        Cholesky decomposition of among-column covariance matrix. Exactly one
        of colcov or colchol is needed.

    Examples
    --------
    Define a matrixvariate normal variable for given row and column covariance
    matrices::

        colcov = np.array([[1., 0.5], [0.5, 2]])
        rowcov = np.array([[1, 0, 0], [0, 4, 0], [0, 0, 16]])
        m = rowcov.shape[0]
        n = colcov.shape[0]
        mu = np.zeros((m, n))
        vals = pm.MatrixNormal('vals', mu=mu, colcov=colcov,
                               rowcov=rowcov, shape=(m, n))

    Above, the ith row in vals has a variance that is scaled by 4^i.
    Alternatively, row or column cholesky matrices could be substituted for
    either covariance matrix. The MatrixNormal is quicker way compute
    MvNormal(mu, np.kron(rowcov, colcov)) that takes advantage of kronecker product
    properties for inversion. For example, if draws from MvNormal had the same
    covariance structure, but were scaled by different powers of an unknown
    constant, both the covariance and scaling could be learned as follows
    (see the docstring of `LKJCholeskyCov` for more information about this)

    .. code:: python

        # Setup data
        true_colcov = np.array([[1.0, 0.5, 0.1],
                                [0.5, 1.0, 0.2],
                                [0.1, 0.2, 1.0]])
        m = 3
        n = true_colcov.shape[0]
        true_scale = 3
        true_rowcov = np.diag([true_scale**(2*i) for i in range(m)])
        mu = np.zeros((m, n))
        true_kron = np.kron(true_rowcov, true_colcov)
        data = np.random.multivariate_normal(mu.flatten(), true_kron)
        data = data.reshape(m, n)

        with pm.Model() as model:
            # Setup right cholesky matrix
            sd_dist = pm.HalfCauchy.dist(beta=2.5, shape=3)
            colchol_packed = pm.LKJCholeskyCov('colcholpacked', n=3, eta=2,
                                               sd_dist=sd_dist)
            colchol = pm.expand_packed_triangular(3, colchol_packed)

            # Setup left covariance matrix
            scale = pm.Lognormal('scale', mu=np.log(true_scale), sigma=0.5)
            rowcov = at.diag([scale**(2*i) for i in range(m)])

            vals = pm.MatrixNormal('vals', mu=mu, colchol=colchol, rowcov=rowcov,
                                   observed=data, shape=(m, n))
    """

    def __init__(
        self,
        mu=0,
        rowcov=None,
        rowchol=None,
        rowtau=None,
        colcov=None,
        colchol=None,
        coltau=None,
        shape=None,
        *args,
        **kwargs,
    ):
        self._setup_matrices(colcov, colchol, coltau, rowcov, rowchol, rowtau)
        if shape is None:
            raise TypeError("shape is a required argument")
        assert len(shape) == 2, "shape must have length 2: mxn"
        self.shape = shape
        super().__init__(shape=shape, *args, **kwargs)
        self.mu = at.as_tensor_variable(mu)
        self.mean = self.median = self.mode = self.mu
        self.solve_lower = solve_lower_triangular
        self.solve_upper = solve_upper_triangular

    def _setup_matrices(self, colcov, colchol, coltau, rowcov, rowchol, rowtau):
        cholesky = Cholesky(lower=True, on_error="raise")

        # Among-row matrices
        if len([i for i in [rowtau, rowcov, rowchol] if i is not None]) != 1:
            raise ValueError(
                "Incompatible parameterization. "
                "Specify exactly one of rowtau, rowcov, "
                "or rowchol."
            )
        if rowcov is not None:
            self.m = rowcov.shape[0]
            self._rowcov_type = "cov"
            rowcov = at.as_tensor_variable(rowcov)
            if rowcov.ndim != 2:
                raise ValueError("rowcov must be two dimensional.")
            self.rowchol_cov = cholesky(rowcov)
            self.rowcov = rowcov
        elif rowtau is not None:
            raise ValueError("rowtau not supported at this time")
            self.m = rowtau.shape[0]
            self._rowcov_type = "tau"
            rowtau = at.as_tensor_variable(rowtau)
            if rowtau.ndim != 2:
                raise ValueError("rowtau must be two dimensional.")
            self.rowchol_tau = cholesky(rowtau)
            self.rowtau = rowtau
        else:
            self.m = rowchol.shape[0]
            self._rowcov_type = "chol"
            if rowchol.ndim != 2:
                raise ValueError("rowchol must be two dimensional.")
            self.rowchol_cov = at.as_tensor_variable(rowchol)

        # Among-column matrices
        if len([i for i in [coltau, colcov, colchol] if i is not None]) != 1:
            raise ValueError(
                "Incompatible parameterization. "
                "Specify exactly one of coltau, colcov, "
                "or colchol."
            )
        if colcov is not None:
            self.n = colcov.shape[0]
            self._colcov_type = "cov"
            colcov = at.as_tensor_variable(colcov)
            if colcov.ndim != 2:
                raise ValueError("colcov must be two dimensional.")
            self.colchol_cov = cholesky(colcov)
            self.colcov = colcov
        elif coltau is not None:
            raise ValueError("coltau not supported at this time")
            self.n = coltau.shape[0]
            self._colcov_type = "tau"
            coltau = at.as_tensor_variable(coltau)
            if coltau.ndim != 2:
                raise ValueError("coltau must be two dimensional.")
            self.colchol_tau = cholesky(coltau)
            self.coltau = coltau
        else:
            self.n = colchol.shape[0]
            self._colcov_type = "chol"
            if colchol.ndim != 2:
                raise ValueError("colchol must be two dimensional.")
            self.colchol_cov = at.as_tensor_variable(colchol)

    def random(self, point=None, size=None):
        """
        Draw random values from Matrix-valued Normal distribution.

        Parameters
        ----------
        point: dict, optional
            Dict of variable values on which random values are to be
            conditioned (uses default point if not specified).
        size: int, optional
            Desired size of random sample (returns one sample if not
            specified).

        Returns
        -------
        array
        """
        # mu, colchol, rowchol = draw_values(
        #     [self.mu, self.colchol_cov, self.rowchol_cov], point=point, size=size
        # )
        # size = to_tuple(size)
        # dist_shape = to_tuple(self.shape)
        # output_shape = size + dist_shape
        #
        # # Broadcasting all parameters
        # (mu,) = broadcast_dist_samples_to(to_shape=output_shape, samples=[mu], size=size)
        # rowchol = np.broadcast_to(rowchol, shape=size + rowchol.shape[-2:])
        #
        # colchol = np.broadcast_to(colchol, shape=size + colchol.shape[-2:])
        # colchol = np.swapaxes(colchol, -1, -2)  # Take transpose
        #
        # standard_normal = np.random.standard_normal(output_shape)
        # samples = mu + np.matmul(rowchol, np.matmul(standard_normal, colchol))
        # return samples

    def _trquaddist(self, value):
        """Compute Tr[colcov^-1 @ (x - mu).T @ rowcov^-1 @ (x - mu)] and
        the logdet of colcov and rowcov."""

        delta = value - self.mu
        rowchol_cov = self.rowchol_cov
        colchol_cov = self.colchol_cov

        # Find exponent piece by piece
        right_quaddist = self.solve_lower(rowchol_cov, delta)
        quaddist = at.nlinalg.matrix_dot(right_quaddist.T, right_quaddist)
        quaddist = self.solve_lower(colchol_cov, quaddist)
        quaddist = self.solve_upper(colchol_cov.T, quaddist)
        trquaddist = at.nlinalg.trace(quaddist)

        coldiag = at.diag(colchol_cov)
        rowdiag = at.diag(rowchol_cov)
        half_collogdet = at.sum(at.log(coldiag))  # logdet(M) = 2*Tr(log(L))
        half_rowlogdet = at.sum(at.log(rowdiag))  # Using Cholesky: M = L L^T
        return trquaddist, half_collogdet, half_rowlogdet

    def logp(self, value):
        """
        Calculate log-probability of Matrix-valued Normal distribution
        at specified value.

        Parameters
        ----------
        value: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        trquaddist, half_collogdet, half_rowlogdet = self._trquaddist(value)
        m = self.m
        n = self.n
        norm = -0.5 * m * n * pm.floatX(np.log(2 * np.pi))
        return norm - 0.5 * trquaddist - m * half_collogdet - n * half_rowlogdet

    def _distr_parameters_for_repr(self):
        mapping = {"tau": "tau", "cov": "cov", "chol": "chol_cov"}
        return ["mu", "row" + mapping[self._rowcov_type], "col" + mapping[self._colcov_type]]


class KroneckerNormal(Continuous):
    r"""
    Multivariate normal log-likelihood with Kronecker-structured covariance.

    .. math::

       f(x \mid \mu, K) =
           \frac{1}{(2\pi |K|)^{1/2}}
           \exp\left\{ -\frac{1}{2} (x-\mu)^{\prime} K^{-1} (x-\mu) \right\}

    ========  ==========================
    Support   :math:`x \in \mathbb{R}^N`
    Mean      :math:`\mu`
    Variance  :math:`K = \bigotimes K_i` + \sigma^2 I_N
    ========  ==========================

    Parameters
    ----------
    mu: array
        Vector of means, just as in `MvNormal`.
    covs: list of arrays
        The set of covariance matrices :math:`[K_1, K_2, ...]` to be
        Kroneckered in the order provided :math:`\bigotimes K_i`.
    chols: list of arrays
        The set of lower cholesky matrices :math:`[L_1, L_2, ...]` such that
        :math:`K_i = L_i L_i'`.
    evds: list of tuples
        The set of eigenvalue-vector, eigenvector-matrix pairs
        :math:`[(v_1, Q_1), (v_2, Q_2), ...]` such that
        :math:`K_i = Q_i \text{diag}(v_i) Q_i'`. For example::

            v_i, Q_i = at.nlinalg.eigh(K_i)

    sigma: scalar, variable
        Standard deviation of the Gaussian white noise.

    Examples
    --------
    Define a multivariate normal variable with a covariance
    :math:`K = K_1 \otimes K_2`

    .. code:: python

        K1 = np.array([[1., 0.5], [0.5, 2]])
        K2 = np.array([[1., 0.4, 0.2], [0.4, 2, 0.3], [0.2, 0.3, 1]])
        covs = [K1, K2]
        N = 6
        mu = np.zeros(N)
        with pm.Model() as model:
            vals = pm.KroneckerNormal('vals', mu=mu, covs=covs, shape=N)

    Effeciency gains are made by cholesky decomposing :math:`K_1` and
    :math:`K_2` individually rather than the larger :math:`K` matrix. Although
    only two matrices :math:`K_1` and :math:`K_2` are shown here, an arbitrary
    number of submatrices can be combined in this way. Choleskys and
    eigendecompositions can be provided instead

    .. code:: python

        chols = [np.linalg.cholesky(Ki) for Ki in covs]
        evds = [np.linalg.eigh(Ki) for Ki in covs]
        with pm.Model() as model:
            vals2 = pm.KroneckerNormal('vals2', mu=mu, chols=chols, shape=N)
            # or
            vals3 = pm.KroneckerNormal('vals3', mu=mu, evds=evds, shape=N)

    neither of which will be converted. Diagonal noise can also be added to
    the covariance matrix, :math:`K = K_1 \otimes K_2 + \sigma^2 I_N`.
    Despite the noise removing the overall Kronecker structure of the matrix,
    `KroneckerNormal` can continue to make efficient calculations by
    utilizing eigendecompositons of the submatrices behind the scenes [1].
    Thus,

    .. code:: python

        sigma = 0.1
        with pm.Model() as noise_model:
            vals = pm.KroneckerNormal('vals', mu=mu, covs=covs, sigma=sigma, shape=N)
            vals2 = pm.KroneckerNormal('vals2', mu=mu, chols=chols, sigma=sigma, shape=N)
            vals3 = pm.KroneckerNormal('vals3', mu=mu, evds=evds, sigma=sigma, shape=N)

    are identical, with `covs` and `chols` each converted to
    eigendecompositions.

    References
    ----------
    .. [1] Saatchi, Y. (2011). "Scalable inference for structured Gaussian process models"
    """

    def __init__(self, mu, covs=None, chols=None, evds=None, sigma=None, *args, **kwargs):
        self._setup(covs, chols, evds, sigma)
        super().__init__(*args, **kwargs)
        self.mu = at.as_tensor_variable(mu)
        self.mean = self.median = self.mode = self.mu

    def _setup(self, covs, chols, evds, sigma):
        self.cholesky = Cholesky(lower=True, on_error="raise")
        if len([i for i in [covs, chols, evds] if i is not None]) != 1:
            raise ValueError(
                "Incompatible parameterization. Specify exactly one of covs, chols, or evds."
            )
        self._isEVD = False
        self.sigma = sigma
        self.is_noisy = self.sigma is not None and self.sigma != 0
        if covs is not None:
            self._cov_type = "cov"
            self.covs = covs
            if self.is_noisy:
                # Noise requires eigendecomposition
                eigh_map = map(eigh, covs)
                self._setup_evd(eigh_map)
            else:
                # Otherwise use cholesky as usual
                self.chols = list(map(self.cholesky, self.covs))
                self.chol_diags = list(map(at.diag, self.chols))
                self.sizes = at.as_tensor_variable([chol.shape[0] for chol in self.chols])
                self.N = at.prod(self.sizes)
        elif chols is not None:
            self._cov_type = "chol"
            if self.is_noisy:  # A strange case...
                # Noise requires eigendecomposition
                covs = [at.dot(chol, chol.T) for chol in chols]
                eigh_map = map(eigh, covs)
                self._setup_evd(eigh_map)
            else:
                self.chols = chols
                self.chol_diags = list(map(at.diag, self.chols))
                self.sizes = at.as_tensor_variable([chol.shape[0] for chol in self.chols])
                self.N = at.prod(self.sizes)
        else:
            self._cov_type = "evd"
            self._setup_evd(evds)

    def _setup_evd(self, eigh_iterable):
        self._isEVD = True
        eigs_sep, Qs = zip(*eigh_iterable)  # Unzip
        self.Qs = list(map(at.as_tensor_variable, Qs))
        self.QTs = list(map(at.transpose, self.Qs))

        self.eigs_sep = list(map(at.as_tensor_variable, eigs_sep))
        self.eigs = kron_diag(*self.eigs_sep)  # Combine separate eigs
        if self.is_noisy:
            self.eigs += self.sigma ** 2
        self.N = self.eigs.shape[0]

    def _setup_random(self):
        if not hasattr(self, "mv_params"):
            self.mv_params = {"mu": self.mu}
            if self._cov_type == "cov":
                cov = kronecker(*self.covs)
                if self.is_noisy:
                    cov = cov + self.sigma ** 2 * at.identity_like(cov)
                self.mv_params["cov"] = cov
            elif self._cov_type == "chol":
                if self.is_noisy:
                    covs = []
                    for eig, Q in zip(self.eigs_sep, self.Qs):
                        cov_i = at.dot(Q, at.dot(at.diag(eig), Q.T))
                        covs.append(cov_i)
                    cov = kronecker(*covs)
                    if self.is_noisy:
                        cov = cov + self.sigma ** 2 * at.identity_like(cov)
                    self.mv_params["chol"] = self.cholesky(cov)
                else:
                    self.mv_params["chol"] = kronecker(*self.chols)
            elif self._cov_type == "evd":
                covs = []
                for eig, Q in zip(self.eigs_sep, self.Qs):
                    cov_i = at.dot(Q, at.dot(at.diag(eig), Q.T))
                    covs.append(cov_i)
                cov = kronecker(*covs)
                if self.is_noisy:
                    cov = cov + self.sigma ** 2 * at.identity_like(cov)
                self.mv_params["cov"] = cov

    def random(self, point=None, size=None):
        """
        Draw random values from Multivariate Normal distribution
        with Kronecker-structured covariance.

        Parameters
        ----------
        point: dict, optional
            Dict of variable values on which random values are to be
            conditioned (uses default point if not specified).
        size: int, optional
            Desired size of random sample (returns one sample if not
            specified).

        Returns
        -------
        array
        """
        # Expand params into terms MvNormal can understand to force consistency
        self._setup_random()
        self.mv_params["shape"] = self.shape
        dist = MvNormal.dist(**self.mv_params)
        return dist.random(point, size)

    def _quaddist(self, value):
        """Computes the quadratic (x-mu)^T @ K^-1 @ (x-mu) and log(det(K))"""
        if value.ndim > 2 or value.ndim == 0:
            raise ValueError("Invalid dimension for value: %s" % value.ndim)
        if value.ndim == 1:
            onedim = True
            value = value[None, :]
        else:
            onedim = False

        delta = value - self.mu
        if self._isEVD:
            sqrt_quad = kron_dot(self.QTs, delta.T)
            sqrt_quad = sqrt_quad / at.sqrt(self.eigs[:, None])
            logdet = at.sum(at.log(self.eigs))
        else:
            sqrt_quad = kron_solve_lower(self.chols, delta.T)
            logdet = 0
            for chol_size, chol_diag in zip(self.sizes, self.chol_diags):
                logchol = at.log(chol_diag) * self.N / chol_size
                logdet += at.sum(2 * logchol)
        # Square each sample
        quad = at.batched_dot(sqrt_quad.T, sqrt_quad.T)
        if onedim:
            quad = quad[0]
        return quad, logdet

    def logp(self, value):
        """
        Calculate log-probability of Multivariate Normal distribution
        with Kronecker-structured covariance at specified value.

        Parameters
        ----------
        value: numeric
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """
        quad, logdet = self._quaddist(value)
        return -(quad + logdet + self.N * at.log(2 * np.pi)) / 2.0

    def _distr_parameters_for_repr(self):
        return ["mu"]


class CAR(Continuous):
    r"""
    Likelihood for a conditional autoregression. This is a special case of the
    multivariate normal with an adjacency-structured covariance matrix.

    .. math::

       f(x \mid W, \alpha, \tau) =
           \frac{|T|^{1/2}}{(2\pi)^{k/2}}
           \exp\left\{ -\frac{1}{2} (x-\mu)^{\prime} T^{-1} (x-\mu) \right\}

    where :math:`T = (\tau D(I-\alpha W))^{-1}` and :math:`D = diag(\sum_i W_{ij})`.

    ========  ==========================
    Support   :math:`x \in \mathbb{R}^k`
    Mean      :math:`\mu \in \mathbb{R}^k`
    Variance  :math:`(\tau D(I-\alpha W))^{-1}`
    ========  ==========================

    Parameters
    ----------
    mu: array
        Real-valued mean vector
    W: Numpy matrix
        Symmetric adjacency matrix of 1s and 0s indicating
        adjacency between elements.
    alpha: float or array
        Autoregression parameter taking values between -1 and 1. Values closer to 0 indicate weaker
        correlation and values closer to 1 indicate higher autocorrelation. For most use cases, the
        support of alpha should be restricted to (0, 1)
    tau: float or array
        Positive precision variable controlling the scale of the underlying normal variates.
    sparse: bool, default=False
        Determines whether or not sparse computations are used

    References
    ----------
    ..  Jin, X., Carlin, B., Banerjee, S.
        "Generalized Hierarchical Multivariate CAR Models for Areal Data"
        Biometrics, Vol. 61, No. 4 (Dec., 2005), pp. 950-961
    """

    def __init__(self, mu, W, alpha, tau, sparse=False, *args, **kwargs):
        super().__init__(*args, **kwargs)

        D = W.sum(axis=0)
        d, _ = W.shape

        self.d = d
        self.median = self.mode = self.mean = self.mu = at.as_tensor_variable(mu)
        self.sparse = sparse

        if not W.ndim == 2 or not np.allclose(W, W.T):
            raise ValueError("W must be a symmetric adjacency matrix.")

        if sparse:
            W_sparse = scipy.sparse.csr_matrix(W)
            self.W = aesara.sparse.as_sparse_variable(W_sparse)
        else:
            self.W = at.as_tensor_variable(W)

        # eigenvalues of D^−1/2 * W * D^−1/2
        Dinv_sqrt = np.diag(1 / np.sqrt(D))
        DWD = np.matmul(np.matmul(Dinv_sqrt, W), Dinv_sqrt)
        self.lam = scipy.linalg.eigvalsh(DWD)
        self.D = at.as_tensor_variable(D)

        tau = at.as_tensor_variable(tau)
        if tau.ndim > 0:
            self.tau = tau[:, None]
        else:
            self.tau = tau

        alpha = at.as_tensor_variable(alpha)
        if alpha.ndim > 0:
            self.alpha = alpha[:, None]
        else:
            self.alpha = alpha

    def logp(self, value):
        """
        Calculate log-probability of a CAR-distributed vector
        at specified value. This log probability function differs from
        the true CAR log density (AKA a multivariate normal with CAR-structured
        covariance matrix) by an additive constant.

        Parameters
        ----------
        value: array
            Value for which log-probability is calculated.

        Returns
        -------
        TensorVariable
        """

        if value.ndim == 1:
            value = value[None, :]

        logtau = self.d * at.log(self.tau).sum()
        logdet = at.log(1 - self.alpha.T * self.lam[:, None]).sum()
        delta = value - self.mu

        if self.sparse:
            Wdelta = aesara.sparse.dot(delta, self.W)
        else:
            Wdelta = at.dot(delta, self.W)

        tau_dot_delta = self.D[None, :] * delta - self.alpha * Wdelta
        logquad = (self.tau * delta * tau_dot_delta).sum(axis=-1)
        return bound(
            0.5 * (logtau + logdet - logquad),
            self.alpha >= -1,
            self.alpha <= 1,
            self.tau > 0,
            broadcast_conditions=False,
        )

    def random(self, point=None, size=None):
        raise NotImplementedError("Sampling from a CAR distribution is not supported.")

    def _distr_parameters_for_repr(self):
        return ["mu", "W", "alpha", "tau"]
