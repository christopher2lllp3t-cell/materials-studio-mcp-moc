from __future__ import annotations

import itertools
import math
import re
from typing import Any, Iterable, Sequence


AXES = ("x", "y", "z")


def parse_restricted_triclinic_box(lines: Sequence[str]) -> dict[str, Any] | None:
    """Parse a LAMMPS restricted-triclinic data-file box without dropping tilt.

    Unlike a custom trajectory ``BOX BOUNDS`` record, a data-file header stores
    the restricted-triclinic ``xlo/xhi``, ``ylo/yhi`` and ``zlo/zhi`` values
    directly.  Tilt must therefore be preserved but must not be added to these
    true lengths a second time.
    """
    bounds: dict[str, tuple[float, float]] = {}
    tilt = (0.0, 0.0, 0.0)
    tilt_present = False
    for raw in lines[:150]:
        match = re.match(r"^\s*(\S+)\s+(\S+)\s+([xyz])lo\s+\3hi\s*$", raw)
        if match:
            bounds[match.group(3)] = (float(match.group(1)), float(match.group(2)))
        match = re.match(r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+xy\s+xz\s+yz\s*$", raw)
        if match:
            tilt = tuple(float(match.group(index)) for index in range(1, 4))
            tilt_present = True
    if not bounds:
        return None
    if set(bounds) != set(AXES):
        return {"valid": False, "errors": ["Incomplete LAMMPS simulation box bounds"]}
    values = [value for pair in bounds.values() for value in pair] + list(tilt)
    if not all(math.isfinite(value) for value in values):
        return {"valid": False, "errors": ["Simulation box contains NaN or infinite values"]}
    xlo, xhi = bounds["x"]
    ylo, yhi = bounds["y"]
    zlo, zhi = bounds["z"]
    xy, xz, yz = tilt
    lx, ly, lz = xhi - xlo, yhi - ylo, zhi - zlo
    errors = [f"Invalid {axis} box bounds: {bounds[axis][0]}, {bounds[axis][1]}"
              for axis in AXES if bounds[axis][1] <= bounds[axis][0]]
    if lx <= 0 or ly <= 0 or lz <= 0:
        errors.append("True triclinic box lengths must be positive")
    return {
        "valid": not errors,
        "kind": "restricted_triclinic" if tilt_present else "orthogonal",
        "bounds": {axis: list(bounds[axis]) for axis in AXES},
        "true_origin": [xlo, ylo, zlo],
        "true_lengths": [lx, ly, lz],
        "tilt": {"xy": xy, "xz": xz, "yz": yz},
        "cell_rows": [[lx, 0.0, 0.0], [xy, ly, 0.0], [xz, yz, lz]],
        "volume": lx * ly * lz,
        "errors": errors,
    }


def fractional_to_cartesian(fractional: Sequence[float], box: dict[str, Any]) -> list[float]:
    origin = box["true_origin"]
    rows = box["cell_rows"]
    return [origin[j] + sum(fractional[i] * rows[i][j] for i in range(3)) for j in range(3)]


def cartesian_to_fractional(cartesian: Sequence[float], box: dict[str, Any]) -> list[float]:
    x, y, z = (cartesian[i] - box["true_origin"][i] for i in range(3))
    lx, ly, lz = box["true_lengths"]
    xy, xz, yz = (box["tilt"][key] for key in ("xy", "xz", "yz"))
    sz = z / lz
    sy = (y - sz * yz) / ly
    sx = (x - sy * xy - sz * xz) / lx
    return [sx, sy, sz]


def image_unwrap(cartesian: Sequence[float], image: Sequence[int], box: dict[str, Any]) -> list[float]:
    rows = box["cell_rows"]
    return [cartesian[j] + sum(image[i] * rows[i][j] for i in range(3)) for j in range(3)]


def wrap_cartesian(cartesian: Sequence[float], box: dict[str, Any], periodic_axes: Iterable[str]) -> dict[str, Any]:
    periodic = set(periodic_axes)
    fractional = cartesian_to_fractional(cartesian, box)
    image = [math.floor(value) if AXES[i] in periodic else 0 for i, value in enumerate(fractional)]
    wrapped_fractional = [value - image[i] for i, value in enumerate(fractional)]
    return {
        "wrapped": fractional_to_cartesian(wrapped_fractional, box),
        "image": image,
        "fractional": fractional,
        "wrapped_fractional": wrapped_fractional,
    }


def minimum_image_displacement(first: Sequence[float], second: Sequence[float], box: dict[str, Any],
                               periodic_axes: Iterable[str]) -> list[float]:
    """Return the shortest Cartesian lattice image, including skewed cells."""
    delta = [a - b for a, b in zip(cartesian_to_fractional(first, box), cartesian_to_fractional(second, box))]
    periodic = set(periodic_axes)
    choices = []
    for index, value in enumerate(delta):
        if AXES[index] in periodic:
            nearest = round(value)
            choices.append((nearest - 1, nearest, nearest + 1))
        else:
            choices.append((0,))
    rows = box["cell_rows"]
    candidates = []
    for shift in itertools.product(*choices):
        frac = [delta[i] - shift[i] for i in range(3)]
        cart = [sum(frac[i] * rows[i][j] for i in range(3)) for j in range(3)]
        candidates.append(cart)
    return min(candidates, key=lambda vector: sum(value * value for value in vector))
