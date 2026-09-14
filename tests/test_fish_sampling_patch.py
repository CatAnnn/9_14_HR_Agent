"""Execute the real patch's top-p block without a Torch/GPU dependency.

The small vector adapter checks top-p masking and probability reuse, not GPU
precision or multinomial equivalence. Those need the separate real Torch CPU
matrix and end-to-end model checks; structural assertions alone do not suffice.
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path
import textwrap
from types import SimpleNamespace

import pytest


PATCH = Path(__file__).resolve().parents[1] / "fish_tts/patches/vllm_omni_0_24_fish_sampling.patch"


def _top_p_block(*, after):
    lines = [line[1:] for line in PATCH.read_text().splitlines()
             if not line.startswith(("---", "+++")) and line.startswith((" ", "+" if after else "-"))]
    start = next(i for i, line in enumerate(lines) if line.strip() == "if top_p < 1.0:")
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "probs = F.softmax(scaled, dim=-1)")
    return textwrap.dedent("\n".join(lines[start:end]))


class _Vector(list):
    def __sub__(self, other):
        self.subtracted = other
        return _Vector(a - b for a, b in zip(self, other))

    def __ge__(self, threshold):
        return [value >= threshold for value in self]

    def __setitem__(self, mask, value):
        for index, remove in enumerate(mask):
            if remove:
                list.__setitem__(self, index, value)

    def scatter(self, dim, indices, source):
        assert dim == 1
        result = [None] * len(self)
        for index, value in zip(indices, source):
            result[index] = value
        return _Vector(result)


def _run(values, top_p, *, after):
    events = {"softmax": [], "cumsum": []}

    def softmax(values, *, dim):
        assert dim == -1
        unnormalized = [math.exp(value - max(values)) for value in values]
        result = _Vector(value / sum(unnormalized) for value in unnormalized)
        events["softmax"].append(result)
        return result

    def cumsum(values, *, dim):
        assert dim == -1
        result = _Vector(itertools.accumulate(values))
        events["cumsum"].append((values, result))
        return result

    def sort(values, *, descending):
        indices = sorted(range(len(values)), key=values.__getitem__, reverse=descending)
        return _Vector(values[index] for index in indices), indices

    namespace = {"torch": SimpleNamespace(sort=sort, cumsum=cumsum), "F": SimpleNamespace(softmax=softmax)}
    source = "def apply(scaled, top_p):\n" + textwrap.indent(_top_p_block(after=after), "    ") + "\n    return scaled\n"
    exec(compile(source, str(PATCH), "exec"), namespace)
    return namespace["apply"](_Vector(values), top_p), events


@pytest.mark.parametrize("values", [[-1.0, 2.0, 0.0, 2.0], [0.0] * 4,
                                   [float("-inf"), 0.0, -10.0, float("-inf")]])
@pytest.mark.parametrize("top_p", [0.01, 0.25, 0.5, 0.9, 1.0])
def test_actual_patch_preserves_sorted_mask_and_scatter(values, top_p):
    before, old_events = _run(values, top_p, after=False)
    after, new_events = _run(values, top_p, after=True)
    assert after == before
    assert len(new_events["softmax"]) == (1 if top_p < 1.0 else 0)
    assert len(old_events["softmax"]) == (2 if top_p < 1.0 else 0)
    if top_p < 1.0:
        probability, cumulative = new_events["cumsum"][0]
        assert probability is new_events["softmax"][0]
        assert cumulative.subtracted is probability


def test_exact_threshold_keeps_the_token_crossing_the_nucleus_boundary():
    result, _ = _run([0.0] * 4, 0.5, after=True)
    assert result == [0.0, 0.0, float("-inf"), float("-inf")]


def test_scatter_restores_the_original_unsorted_vocabulary_positions():
    result, _ = _run([math.log(0.1), math.log(0.7), math.log(0.2)], 0.6, after=True)
    assert result == [float("-inf"), math.log(0.7), float("-inf")]


def test_tiny_positive_nucleus_keeps_at_least_the_largest_token():
    result, _ = _run([-1000.0, 10.0, 0.0], 0.00001, after=True)
    assert result == [float("-inf"), 10.0, float("-inf")]
