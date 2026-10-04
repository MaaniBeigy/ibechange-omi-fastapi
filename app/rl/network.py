"""Adam/ReLU network extracted from notification-reward-engineering.

SPDX-License-Identifier: GPL-3.0-only
Source: src/notifsim/agents/ddqn.py; see NOTICE.md in this directory.
"""

import numpy as np

BETA1, BETA2, EPSILON = 0.9, 0.999, 1e-8


class Network:
    """A rectified network of one or more hidden layers, trained by Adam."""

    def __init__(self, width: int, hidden: int, rng, layers: int = 1, actions: int = 2):
        sizes = [width] + [hidden] * int(layers) + [int(actions)]
        self.weights = [
            rng.normal(0.0, np.sqrt(2.0 / a), (a, b)) for a, b in zip(sizes, sizes[1:])
        ]
        # The output layer starts at zero, so an untrained network returns its output
        # bias and nothing else. Random weights there give every state a different
        # arbitrary preference before any data has been seen, and on a 24-field state
        # they reach a size no starting bias can answer. The first step makes the layer
        # nonzero and the hidden layers learn from the second on.
        self.weights[-1][...] = 0.0
        self.biases = [np.zeros(b) for b in sizes[1:]]
        self.moment = [np.zeros_like(p) for p in self.parameters()]
        self.velocity = [np.zeros_like(p) for p in self.parameters()]
        self.updates = 0

    @property
    def bias_out(self) -> np.ndarray:
        """Return the output bias, which a test sets directly to fix a preference."""
        return self.biases[-1]

    @property
    def weight_out(self) -> np.ndarray:
        """Return the output weights."""
        return self.weights[-1]

    def parameters(self) -> list:
        """Return the trainable arrays, in the order the gradients come back in."""
        return [array for pair in zip(self.weights, self.biases) for array in pair]

    def forward(self, features):
        """Return the action values and every hidden activation behind them."""
        activation, hidden = features, []
        for index, (weight, bias) in enumerate(zip(self.weights, self.biases)):
            activation = activation @ weight + bias
            if index < len(self.weights) - 1:
                activation = np.maximum(activation, 0.0)
                hidden.append(activation)
        return activation, hidden

    def values(self, features) -> np.ndarray:
        """Return the action values alone."""
        return self.forward(np.atleast_2d(features))[0]

    def backward(self, features, hidden, upstream) -> list:
        """Return the gradient of each parameter for one batch."""
        inputs = [features] + list(hidden)
        gradients = []
        for index in range(len(self.weights) - 1, -1, -1):
            gradients = [inputs[index].T @ upstream, upstream.sum(axis=0)] + gradients
            if index:
                upstream = (upstream @ self.weights[index].T) * (hidden[index - 1] > 0)
        return gradients

    def apply(self, gradients: list, rate: float) -> None:
        """Take one Adam step against the gradients."""
        self.updates += 1
        correction_m = 1.0 - BETA1**self.updates
        correction_v = 1.0 - BETA2**self.updates
        for index, (parameter, gradient) in enumerate(
            zip(self.parameters(), gradients)
        ):
            self.moment[index] = BETA1 * self.moment[index] + (1.0 - BETA1) * gradient
            self.velocity[index] = (
                BETA2 * self.velocity[index] + (1.0 - BETA2) * gradient**2
            )
            step = (self.moment[index] / correction_m) / (
                np.sqrt(self.velocity[index] / correction_v) + EPSILON
            )
            parameter -= rate * step

    def copy_into(self, other) -> None:
        """Overwrite another network's weights with these."""
        for target, source in zip(other.parameters(), self.parameters()):
            target[...] = source
