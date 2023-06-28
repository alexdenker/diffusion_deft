r"""
Learned Iterative Soft-Thresholding Algorithm (LISTA) for compressed sensing
====================================================================================================

This example shows how to implement the `LISTA <http://yann.lecun.com/exdb/publis/pdf/gregor-icml-10.pdf>`_ algorithm
for a compressed sensing problem. In a nutshell, LISTA is an unfolded proximal gradient algorithm involving a
soft-thresholding proximal operator with learnable thresholding parameters.

"""
from pathlib import Path

import numpy as np
import torch
from torchvision import datasets
from torchvision import transforms

import deepinv as dinv
from torch.utils.data import DataLoader
from deepinv.optim.data_fidelity import L2
from deepinv.optim.prior import PnP
from deepinv.unfolded import unfolded_builder
from deepinv.training_utils import train, test

import matplotlib.pyplot as plt

# %%
# Setup paths for data loading and results.
# -----------------------------------------
#

BASE_DIR = Path(".")
ORIGINAL_DATA_DIR = BASE_DIR / "datasets"
DATA_DIR = BASE_DIR / "measurements"
RESULTS_DIR = BASE_DIR / "results"
CKPT_DIR = BASE_DIR / "ckpts"

# Set the global random seed from pytorch to ensure reproducibility of the example.
torch.manual_seed(0)

device = dinv.utils.get_freer_gpu() if torch.cuda.is_available() else "cpu"

# %%
# Load base image datasets and degradation operators.
# ----------------------------------------------------------------------------------------
# In this example, we use MNIST as the base dataset.

img_size = 28
n_channels = 1
operation = "compressed-sensing"
train_dataset_name = "MNIST_train"

# Generate training and evaluation datasets in HDF5 folders and load them.
train_test_transform = transforms.Compose([transforms.ToTensor()])
train_base_dataset = datasets.MNIST(
    root=ORIGINAL_DATA_DIR, train=True, transform=train_test_transform, download=True
)
test_base_dataset = datasets.MNIST(
    root=ORIGINAL_DATA_DIR, train=False, transform=train_test_transform, download=True
)


# %%
# Generate a dataset of compressed measurements and load it.
# ----------------------------------------------------------------------------
# We use the compressed sensing class from the physics module to generate a dataset of highly-compressed measurements
# (10% of the total number of pixels).
#
# The forward operator is defined as :math:`y = Ax`
# where :math:`A` is a (normalized) random Gaussian matrix.


# Use parallel dataloader if using a GPU to fasten training, otherwise, as all computes are on CPU, use synchronous
# data loading.
num_workers = 4 if torch.cuda.is_available() else 0

# Generate the compressed sensing measurement operator with 10x under-sampling factor.
physics = dinv.physics.CompressedSensing(
    m=78, img_shape=(n_channels, img_size, img_size), device=device
)
my_dataset_name = "demo_LISTA"
n_images_max = (
    1000 if torch.cuda.is_available() else 200
)  # maximal number of images used for training
measurement_dir = DATA_DIR / train_dataset_name / operation
generated_datasets_path = dinv.datasets.generate_dataset(
    train_dataset=train_base_dataset,
    test_dataset=test_base_dataset,
    physics=physics,
    device=device,
    save_dir=measurement_dir,
    train_datapoints=n_images_max,
    test_datapoints=8,
    num_workers=num_workers,
    dataset_filename=str(my_dataset_name),
)

train_dataset = dinv.datasets.HDF5Dataset(path=generated_datasets_path, train=True)
test_dataset = dinv.datasets.HDF5Dataset(path=generated_datasets_path, train=False)

# %%
# Define the unfolded Proximal Gradient algorithm.
# ------------------------------------------------------------------------
# In this example, following the original `LISTA algorithm <http://yann.lecun.com/exdb/publis/pdf/gregor-icml-10.pdf>`_,
# the backbone algorithm we unfold is the proximal gradient algorithm which minimizes the following objective function
#
# .. math::
#
#          \min_x \frac{\lambda}{2} \|y - Ax\|_2^2 + \|Wx\|_1
#
# where :math:`\lambda` is the regularization parameter.
# The proximal gradient iteration (see also :class:`deepinv.optim.optim_iterators.PGDIteration`) is defined as
#
#   .. math::
#           x_{k+1} = \text{prox}_{\gamma g}(x_k - \gamma \lambda A^T (Ax_k - y))
#
# where :math:`\gamma` is the stepsize and :math:`\text{prox}_{g}` is the proximity operator of :math:`g(x) = \|Wx\|_1`
# which corresponds to soft-thresholding with a wavelet basis (see :class:`deepinv.models.WaveletDict`).
#
# We use :class:`deepinv.unfolded.Unfolded` to define the unfolded algorithm
# and set both the stepsizes of the LISTA algorithm :math:`\gamma` (``stepsize``) and the soft
# thresholding parameters :math:`\lambda` (``1/g_param``) as learnable parameters.
# These parameters are initialized with a table of length max_iter,
# yielding a distinct ``stepsize`` and ``g_param`` value for each iteration of the algorithm.

# Select the data fidelity term
data_fidelity = L2()

# Set up the trainable denoising prior; here, the soft-threshold in a wavelet basis.
# If the prior is initialized with a list of length max_iter,
# then a distinct weight is trained for each PGD iteration.
# For fixed trained model prior across iterations, initialize with a single model.
max_iter = 30 if torch.cuda.is_available() else 10  # Number of unrolled iterations
level = 2
prior = [
    PnP(denoiser=dinv.models.WaveletPrior(wv="db8", level=level).to(device))
    for i in range(max_iter)
]

# Unrolled optimization algorithm parameters

lamb = [1.0] * max_iter  # initialization of the regularization parameter.
# A distinct lamb is trained for each iteration.

stepsize = [1.0] * max_iter  # initialization of the stepsizes.
# A distinct stepsize is trained for each iteration.

sigma_denoiser_init = 0.01
sigma_denoiser = [sigma_denoiser_init * torch.ones(level, 3)] * max_iter
# A distinct sigma_denoiser is trained for each iteration.

params_algo = {  # wrap all the restoration parameters in a 'params_algo' dictionary
    "stepsize": stepsize,
    "g_param": sigma_denoiser,
    "lambda": lamb,
}

trainable_params = [
    "g_param",
    "stepsize",
    "lambda",
]  # define which parameters from 'params_algo' are trainable

# Define the unfolded trainable model.
model = unfolded_builder(
    iteration="PGD",
    params_algo=params_algo.copy(),
    trainable_params=trainable_params,
    data_fidelity=data_fidelity,
    max_iter=max_iter,
    prior=prior,
)


# %%
# Define the training parameters.
# -------------------------------
#
# We now define training-related parameters,
# number of epochs, optimizer (Adam) and its hyperparameters, and the train and test batch sizes.
#


# Training parameters
epochs = 20 if torch.cuda.is_available() else 5
learning_rate = 1e-3

# Choose optimizer and scheduler
optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=0.0)

# Choose supervised training loss
losses = [dinv.loss.SupLoss(metric=dinv.metric.mse())]

# Logging parameters
verbose = True
wandb_vis = False  # plot curves and images in Weight&Bias

# Batch sizes and data loaders
train_batch_size = 64 if torch.cuda.is_available() else 1
test_batch_size = 64 if torch.cuda.is_available() else 8

train_dataloader = DataLoader(
    train_dataset, batch_size=train_batch_size, num_workers=num_workers, shuffle=True
)
test_dataloader = DataLoader(
    test_dataset, batch_size=test_batch_size, num_workers=num_workers, shuffle=False
)

# %%
# Train the network.
# -------------------------------------------
#
# We train the network using the library's train function.
#

train(
    model=model,
    train_dataloader=train_dataloader,
    eval_dataloader=test_dataloader,
    epochs=epochs,
    losses=losses,
    physics=physics,
    optimizer=optimizer,
    device=device,
    save_path=str(CKPT_DIR / operation),
    verbose=verbose,
    wandb_vis=wandb_vis,
)

# %%
# Test the network.
# ---------------------------
#
# We now test the learned unrolled network on the test dataset. In the plotted results, the `Linear` column shows the
# measurements back-projected in the image domain, the `Recons` column shows the output of our LISTA network,
# and `GT` shows the ground truth.
#

plot_images = True
method = "unfolded_pgd"

test(
    model=model,
    test_dataloader=test_dataloader,
    physics=physics,
    device=device,
    plot_images=plot_images,
    save_folder=RESULTS_DIR / method / operation,
    verbose=verbose,
    wandb_vis=wandb_vis,
)


# %%
# Plotting the learned parameters.
# ------------------------------------
dinv.utils.plotting.plot_parameters(
    model, init_params=params_algo, save_dir=RESULTS_DIR / method / operation
)