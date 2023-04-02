import torch
import numpy as np
from deepinv.optim.utils import conjugate_gradient
from .noise import GaussianNoise


class Physics(torch.nn.Module):  # parent class for forward models
    r'''
    Parent class for forward operators

    It describes the general forward measurement process

    .. math::

        y = N(A(x))

    where :math:`x` is an image of :math:`n` pixels, :math:`y` is the measurements of size :math:`m`,
    :math:`A:\mathbb{R}^{n}\mapsto \mathbb{R}^{m}` is a deterministic mapping capturing the physics of the acquisition
    and :math:`N:\mathbb{R}^{m}\mapsto \mathbb{R}^{m}` is a stochastic mapping which characterizes the noise affecting
    the measurements.

    :param callable A: forward operator function which maps an image to the observed measurements :math:`x\mapsto y`.
        If A is linear, it is recommended to normalize it to have unit norm, which can be verified via
        ``self.adjointness_test``.
    :param callable A_adjoint: transpose of the forward operator, which maps measurement to the image space.
        If the operator is linear, A_adjoint should verify the adjointness test.
    :param callable noise_model: function that adds noise to the measurements :math:`N(z)`.
        See the noise module for some predefined functions.
    :param callable sensor_model: function that incorporates any sensor non-linearities to the sensing process,
        such as quantization or saturation, defined as a function :math:`\eta(z)`, such that
        :math:`y=\eta\left(N(A(x))\right)`. By default, the sensor_model is set to the identity :math:`\eta(z)=z`.
    :param int max_iter: If the operator does not have a closed form pseudoinverse, the conjugate gradient algorithm
        is used for computing it, and this parameter fixes the maximum number of conjugate gradient iterations.
    :param float tol: If the operator does not have a closed form pseudoinverse, the conjugate gradient algorithm
        is used for computing it, and this parameter fixes the absolute tolerance of the conjugate gradient algorithm.

    '''
    def __init__(self, A=lambda x: x, A_adjoint=lambda x: x,
                 noise_model=lambda x: x, sensor_model=lambda x: x,
                 max_iter=50, tol=1e-3):
        super().__init__()
        self.noise_model = noise_model
        self.sensor_model = sensor_model
        self.forw = A
        self.SVD = False  # flag indicating SVD available
        self.adjoint = A_adjoint
        self.max_iter = max_iter
        self.tol = tol

    def __add__(self, other): #  physics3 = physics1 + physics2
        r'''
        Concatenates two forward operators :math:`A = A_1\circ A_2` via the add operation

        The resulting operator keeps the noise and sensor models of :math:`A_1`.

        :param deepinv.Physics other: Physics operator :math:`A_2`
        :return: (deepinv.Physics) concantenated operator

        '''
        A = lambda x: self.A(other.A(x)) # (A' = A_1 A_2)
        A_adjoint = lambda x: other.A_adjoint(self.A_adjoint(x))
        noise = self.noise_model
        sensor = self.sensor_model
        return Physics(A, A_adjoint, noise, sensor)

    def forward(self, x):
        r'''
        Computes forward operator :math:`y = N(A(x))` (with noise and/or sensor non-linearities)

        :param torch.tensor,list of torch.tensor x: signal/image
        :return: (torch.tensor) noisy measurements

        '''
        return self.sensor(self.noise(self.A(x)))

    def A(self, x):
        r'''
        Computes forward operator :math:`y = A(x)` (without noise and/or sensor non-linearities)

        :param torch.tensor,list of torch.tensor x: signal/image
        :return: (torch.tensor) clean measurements

        '''
        return self.forw(x)

    def sensor(self, x):
        r'''
        Computes sensor non-linearities :math:`y = \eta(y)`

        :param torch.tensor,list of torch.tensor x: signal/image
        :return: (torch.tensor) clean measurements
        '''
        return self.sensor_model(x)

    def noise(self, x):
        r'''
        Incorporates noise into the measurements :math:`\tilde{y} = N(y)`

        :param torch.tensor x:  clean measurements
        :return torch.tensor: noisy measurements

        '''
        return self.noise_model(x)

    def A_adjoint(self, x):
        r'''
        Computes transpose of the forward operator :math:`\tilde{x} = A^{\top}y`.
        If A is linear, it should be the exact transpose of the forward matrix.

        .. note:

            If problem is non-linear, there is not a well-defined transpose operation,
            but defining one can be useful for some reconstruction networks, such as ``deepinv.models.ArtifactRemoval``.

        :param torch.tensor x: noiseless measurements
        :return: (torch.tensor) or list of (torch.tensors), describing the linearly reconstructed signal/image

        '''
        return self.adjoint(x)

    def prox_l2(self, z, y, gamma):
        r'''
        Computes proximal operator of :math:`f(x) = \frac{1}{2}\|Ax-y\|^2`, i.e.,

        .. math::

            \underset{x}{\arg\min} \; \frac{1}{2} \|Ax-y\|^2 + \frac{\gamma}{2}\|x-z\|^2

        :param torch.tensor y: measurements tensor
        :param torch.tensor z: signal tensor
        :param float gamma: hyperparameter of the proximal operator
        :return: (torch.tensor) estimated signal tensor

        '''
        b = self.A_adjoint(y) + gamma*z
        H = lambda x: self.A_adjoint(self.A(x))+gamma*x
        x = conjugate_gradient(H, b, self.max_iter, self.tol)
        return x

    # def prox_l2_norm(self, z, y, gamma):
    #     r'''
    #
    #     Computes the proximal operator of :math:`f(x) = \frac{1}{2}\gamma \|x-y\|_2^2`
    #
    #     :param torch.tensor y: measurements tensor
    #     :param torch.tensor z: signal tensor
    #     :param float gamma: hyperparameter of the proximal operator
    #     :return: (torch.tensor) estimated signal tensor
    #
    #     '''
    #     return (z + gamma * y) / (1 + gamma)

    def A_dagger(self, y):
        r'''
        Computes :math:`A^{\dagger}y = x` using the conjugate gradient method https://en.wikipedia.org/wiki/Conjugate_gradient_method.

        If the size of :math:`y` is larger than :math:`x` (overcomplete problem), it computes :math:`(A^{\top} A)^{-1} A^{\top} y`,
        otherwise (incomplete problem) it computes :math:`A^{\top} (A A^{\top})^{-1} y`.

        This function can be overwritten by a more efficient pseudoinverse in cases where closed form formulas exist.

        :param torch.tensor y: a measurement :math:`y` to reconstruct via the pseudoinverse.
        :return: (torch.tensor) The reconstructed image :math:`x`.

        '''
        Aty = self.A_adjoint(y)

        overcomplete = np.prod(Aty.shape) < np.prod(y.shape)

        if not overcomplete:
            A = lambda x: self.A(self.A_adjoint(x))
            b = y
        else:
            A = lambda x: self.A_adjoint(self.A(x))
            b = Aty

        x = conjugate_gradient(A=A, b=b, max_iter=self.max_iter, tol=self.tol)

        if not overcomplete:
            x = self.A_adjoint(x)

        return x

    def compute_norm(self, x0, max_iter=100, tol=1e-3, verbose=True):
        r'''
        Computes the spectral :math:`\ell_2` norm (Lipschitz constant) of the operator

        :math:`A^{\top}A`, i.e., :math:`\|A^{\top}A\|`.

        using the power method https://en.wikipedia.org/wiki/Power_iteration.

        :param torch.tensor x0: initialisation point of the algorithm
        :param int max_iter: maximum number of iterations
        :param float tol: relative variation criterion for convergence
        :param bool verbose: print information

        :returns z: (float) spectral norm of :math:`A^{\top}A`, i.e., :math:`\|A^{\top}A\|`.
        '''
        x = torch.randn_like(x0)
        x /= torch.norm(x)
        zold = torch.zeros_like(x)
        for it in range(max_iter):
            y = self.A(x)
            y = self.A_adjoint(y)
            z = torch.matmul(x.reshape(-1), y.reshape(-1)) / torch.norm(x) ** 2

            rel_var = torch.norm(z - zold)
            if rel_var < tol and verbose:
                print(f"Power iteration converged at iteration {it}, value={z.item():.2f}")
                break
            zold = z
            x = y / torch.norm(y)

        return z

    def adjointness_test(self, u):
        r'''
        Numerically check that :math:`A^{\top}` is indeed the adjoint of :math:`A`.

        :param torch.tensor u: initialisation point of the adjointness test method

        :return: (float) a quantity that should be theoretically 0. In practice, it should be of the order of the chosen dtype precision (i.e. single or double).

        '''
        u_in = u #.type(self.dtype)
        Au = self.A(u_in)

        v = torch.randn_like(Au)
        Atv = self.A_adjoint(v)

        s1 = (v*Au).flatten().sum()
        s2 = (Atv*u_in).flatten().sum()

        return s1-s2


class DecomposablePhysics(Physics):
    r'''
    Parent class for linear operators with SVD decomposition.


    The singular value decomposition is expressed as

    .. math::

        A = U\text{diag}(s)V^{\top} \in \mathbb{R}^{m\times n}

    where :math:`U\in\mathbb{C}^{n\times n}` and :math:`V\in\mathbb{C}^{m\times m}`
    are orthonormal linear transformations and :math:`s\in\mathbb{R}_{+}^{n}` are the singular values.

    :param callable U: orthonormal transformation
    :param callable U_adjoint: transpose of U
    :param callable V: orthonormal transformation
    :param callable V_adjoint: transpose of V
    :param torch.tensor, float mask: Singular values of the transform

    '''
    def __init__(self, U=lambda x: x, U_adjoint=lambda x: x, V=lambda x: x, V_adjoint=lambda x: x, mask = 1., **kwargs):
        super().__init__(**kwargs)
        self._V = V
        self._U = U
        self._U_adjoint = U_adjoint
        self._V_adjoint = V_adjoint
        self.mask = mask

    def A(self, x):
        return self.U(self.mask*self.V_adjoint(x))

    def U(self, x):
        return self._U(x)

    def V(self, x):
        return self._U(x)

    def U_adjoint(self, x):
        return self._U_adjoint(x)

    def V_adjoint(self, x):
        return self._V_adjoint(x)

    def A_adjoint(self, y):

        if isinstance(self.mask, float):
            mask = self.mask
        else:
            mask = torch.conj(self.mask)

        return self.V(mask*self.V_adjoint(y))

    def prox_l2(self, z, y, gamma):
        r'''
        Computes proximal operator of :math:`f(x)=\frac{1}{2}\|Ax-y\|^2`
        i.e.

        .. math::

            \underset{x}{\arg\min} \; \frac{1}{2} \|Ax-y\|^2 + \frac{\gamma}{2} \|x-z\|^2

        :param torch.tensor y: measurements tensor
        :param torch.tensor, float z: signal tensor
        :param float gamma: hyperparameter :math:`\gamma` of the proximal operator
        :return: (torch.tensor) estimated signal tensor

        '''
        b = self.A_adjoint(y) + gamma*z

        # scaling = self.mask.pow(2) + gamma
        if not isinstance(self.mask, float):
            scaling = self.mask.pow(2) + gamma
        else:
            scaling = self.mask ** 2 + gamma

        x = self.V(self.V_adjoint(b)/scaling)

        return x

    def A_dagger(self, y):
        r'''
        Computes :math:`A^{\dagger}y = x` in an efficient manner leveraging the singular vector decomposition.

        :param torch.tensor y: a measurement :math:`y` to reconstruct via the pseudoinverse.
        :return: (torch.tensor) The reconstructed image :math:`x`.

        '''

        # avoid division by singular value = 0

        mask = self.mask

        if not isinstance(self.mask, float):
            mask[mask == 0] = 1

        return self.V(self.U_adjoint(y)/mask)


class Denoising(DecomposablePhysics):
    r'''

    Forward operator for denoising problems.

    The linear operator is just the identity mapping :math:`A(x)=x`

    :param torch.nn.Module noise: noise distribution, e.g., ``deepinv.physics.GaussianNoise``, or a user-defined torch.nn.Module.
    '''
    def __init__(self, noise=GaussianNoise(sigma=.1), **kwargs):
        super().__init__(**kwargs)
        self.noise_model = noise