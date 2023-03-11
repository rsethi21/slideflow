"""Reinhard H&E stain normalization."""

from __future__ import division

import cv2
import numpy as np
from typing import Tuple, Dict, Optional
from contextlib import contextmanager

from . import utils as ut
from .utils import lab_split_numpy as lab_split
from .utils import merge_back_numpy as merge_back

# -----------------------------------------------------------------------------

def get_mean_std(I: np.ndarray, mask: bool = False) -> Tuple[np.ndarray, np.ndarray]:
    """Get mean and standard deviation of each channel.

    Args:
        I (np.ndarray): RGB uint8 image.

    Returns:
        A tuple containing

            np.ndarray:     Channel means, shape = (3,)

            np.ndarray:     Channel standard deviations, shape = (3,)
    """
    I1, I2, I3 = lab_split(I)
    if mask:
        ones = np.all(I == 255, axis=2)
        I1, I2, I3 = I1[~ ones], I2[~ ones], I3[~ ones]
    m1, sd1 = cv2.meanStdDev(I1)
    m2, sd2 = cv2.meanStdDev(I2)
    m3, sd3 = cv2.meanStdDev(I3)
    means = m1, m2, m3
    stds = sd1, sd2, sd3
    return np.array(means), np.array(stds)


class ReinhardFastNormalizer:

    preset_tag = 'reinhard_fast'

    def __init__(self):
        """Modified Reinhard H&E stain normalizer without brightness
        standardization (numpy implementation).

        Normalizes an image as defined by:

        Reinhard, Erik, et al. "Color transfer between images." IEEE
        Computer graphics and applications 21.5 (2001): 34-41.

        This implementation does not include the brightness normalization step.

        This normalizer contains inspiration from StainTools by Peter Byfield
        (https://github.com/Peter554/StainTools).
        """
        self.set_fit(**ut.fit_presets[self.preset_tag]['v1'])  # type: ignore
        self._ctx_means = None
        self._ctx_stds = None

    def fit(self, img: np.ndarray, mask: bool = False) -> Tuple[np.ndarray, np.ndarray]:
        """Fit normalizer to a target image.

        Args:
            img (np.ndarray): Target image (RGB uint8) with dimensions W, H, C.

        Returns:
            A tuple containing

                np.ndarray:  Target means (channel means).

                np.ndarray:   Target stds (channel standard deviations).
        """
        img = ut.clip_size(img, 2048)
        means, stds = get_mean_std(img, mask=mask)
        self.set_fit(means, stds)
        return means, stds

    def fit_preset(self, preset: str) -> Dict[str, np.ndarray]:
        """Fit normalizer to a preset in sf.norm.utils.fit_presets.

        Args:
            preset (str): Preset.

        Returns:
            Dict[str, np.ndarray]: Dictionary mapping fit keys to their
            fitted values.
        """
        _fit = ut.fit_presets[self.preset_tag][preset]
        self.set_fit(**_fit)
        return _fit

    def get_fit(self) -> Dict[str, np.ndarray]:
        """Get the current normalizer fit.

        Returns:
            Dict[str, np.ndarray]: Dictionary mapping 'target_means'
            and 'target_stds' to their respective fit values.
        """
        return {
            'target_means': self.target_means,
            'target_stds': self.target_stds
        }

    def _get_mean_std(
        self,
        image: np.ndarray,
        ctx_means: Optional[np.ndarray],
        ctx_stds: Optional[np.ndarray],
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Get means and standard deviations from an image."""
        if ctx_means is None and ctx_stds is not None:
            raise ValueError(
            "If 'ctx_stds' is provided, 'ctx_means' must not be None"
        )
        if ctx_stds is None and ctx_means is not None:
            raise ValueError(
            "If 'ctx_means' is provided, 'ctx_stds' must not be None"
        )
        if ctx_means is not None and ctx_stds is not None:
            return ctx_means, ctx_stds
        elif self._ctx_means is not None and self._ctx_stds is not None:
            return self._ctx_means, self._ctx_stds
        else:
            return get_mean_std(image)

    def set_fit(
        self,
        target_means: np.ndarray,
        target_stds: np.ndarray
    ) -> None:
        """Set the normalizer fit to the given values.

        Args:
            target_means (np.ndarray): Channel means. Must
                have the shape (3,).
            target_stds (np.ndarray): Channel standard deviations. Must
                have the shape (3,).
        """
        target_means = ut._as_numpy(target_means).flatten()
        target_stds = ut._as_numpy(target_stds).flatten()

        if target_means.shape != (3,):
            raise ValueError("target_means must have flattened shape of (3,) - "
                             f"got {target_means.shape}")
        if target_stds.shape != (3,):
            raise ValueError("target_stds must have flattened shape of (3,) - "
                             f"got {target_stds.shape}")

        self.target_means = target_means
        self.target_stds = target_stds

    def transform(
        self,
        I: np.ndarray,
        ctx_means: Optional[np.ndarray] = None,
        ctx_stds: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Normalize an H&E image.

        Args:
            img (np.ndarray): Image, RGB uint8 with dimensions W, H, C.

        Returns:
            np.ndarray: Normalized image.
        """
        if self.target_means is None or self.target_stds is None:
            raise ValueError("Normalizer has not been fit: call normalizer.fit()")

        I1, I2, I3 = lab_split(I)
        means, stds = self._get_mean_std(I, ctx_means, ctx_stds)

        norm1 = ((I1 - means[0]) * (self.target_stds[0] / stds[0])) + self.target_means[0]
        norm2 = ((I2 - means[1]) * (self.target_stds[1] / stds[1])) + self.target_means[1]
        norm3 = ((I3 - means[2]) * (self.target_stds[2] / stds[2])) + self.target_means[2]

        merged = merge_back(norm1, norm2, norm3)
        return merged

    @contextmanager
    def image_context(self, I: np.ndarray):
        self.set_context(I)
        yield
        self.clear_context()

    def set_context(self, I: np.ndarray):
        I = ut.clip_size(I, 2048)
        self._ctx_means, self._ctx_stds = get_mean_std(I, mask=True)

    def clear_context(self):
        self._ctx_means, self._ctx_stds = None, None


class ReinhardNormalizer(ReinhardFastNormalizer):

    preset_tag = 'reinhard'

    def __init__(self) -> None:
        """Reinhard H&E stain normalizer (numpy implementation).

        Normalizes an image as defined by:

        Reinhard, Erik, et al. "Color transfer between images." IEEE
        Computer graphics and applications 21.5 (2001): 34-41.

        This normalizer contains inspiration from StainTools by Peter Byfield
        (https://github.com/Peter554/StainTools).
        """
        super().__init__()
        self.set_fit(**ut.fit_presets[self.preset_tag]['v1'])  # type: ignore

    def fit(self, target: np.ndarray, mask: bool = False) -> Tuple[np.ndarray, np.ndarray]:
        """Fit normalizer to a target image.

        Args:
            img (np.ndarray): Target image (RGB uint8) with dimensions W, H, C.

        Returns:
            A tuple containing

                np.ndarray:  Target means (channel means).

                np.ndarray:   Target stds (channel standard deviations).
        """
        target = ut.clip_size(target, 2048)
        target = ut.standardize_brightness(target, mask=mask)
        return super().fit(target, mask=mask)

    def fit_preset(self, preset: str) -> Dict[str, np.ndarray]:
        """Fit normalizer to a preset in sf.norm.utils.fit_presets.

        Args:
            preset (str): Preset.

        Returns:
            Dict[str, np.ndarray]: Dictionary mapping fit keys to their
            fitted values.
        """
        _fit = ut.fit_presets[self.preset_tag][preset]
        self.set_fit(**_fit)
        return _fit

    def transform(
        self,
        I: np.ndarray,
        ctx_means: Optional[np.ndarray] = None,
        ctx_stds: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Normalize an H&E image.

        Args:
            img (np.ndarray): Image, RGB uint8 with dimensions W, H, C.

        Returns:
            np.ndarray: Normalized image.
        """
        I = ut.standardize_brightness(I)
        return super().transform(I, ctx_means, ctx_stds)

    def set_context(self, I: np.ndarray):
        I = ut.clip_size(I, 2048)
        I = ut.standardize_brightness(I, mask=True)
        super().set_context(I)

    def clear_context(self):
        super().clear_context()


class ReinhardFastMaskNormalizer(ReinhardFastNormalizer):

    preset_tag = 'reinhard_fast'

    def __init__(self, threshold: float = 0.93) -> None:
        """Modified Reinhard H&E stain normalizer only applied to
        non-whitepsace areas (numpy implementation).

        Normalizes an image as defined by:

        Reinhard, Erik, et al. "Color transfer between images." IEEE
        Computer graphics and applications 21.5 (2001): 34-41.

        This "masked" implementation only normalizes non-whitespace areas.

        This normalizer contains inspiration from StainTools by Peter Byfield
        (https://github.com/Peter554/StainTools).

        Args:
            threshold (float): Whitespace fraction threshold, above which
                pixels are masked and not normalized. Defaults to 0.93.
        """
        super().__init__()
        self.threshold = threshold

    def transform(
        self,
        image: np.ndarray,
        ctx_means: Optional[np.ndarray] = None,
        ctx_stds: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Normalize an H&E image.

        Args:
            img (np.ndarray): Image, RGB uint8 with dimensions W, H, C.

        Returns:
            np.ndarray: Normalized image.
        """
        I1, I2, I3 = lab_split(image)
        mask = ((I3 + 128.) / 255. < self.threshold)[:, :, np.newaxis]
        means, stds = self._get_mean_std(image, ctx_means, ctx_stds)
        norm1 = ((I1 - means[0]) * (self.target_stds[0] / stds[0])) + self.target_means[0]
        norm2 = ((I2 - means[1]) * (self.target_stds[1] / stds[1])) + self.target_means[1]
        norm3 = ((I3 - means[2]) * (self.target_stds[2] / stds[2])) + self.target_means[2]
        return np.where(mask, merge_back(norm1, norm2, norm3), image)


class ReinhardMaskNormalizer(ReinhardNormalizer):

    preset_tag = 'reinhard'

    def __init__(self, threshold: float = 0.93) -> None:
        """Modified Reinhard H&E stain normalizer only applied to
        non-whitepsace areas (numpy implementation).

        Normalizes an image as defined by:

        Reinhard, Erik, et al. "Color transfer between images." IEEE
        Computer graphics and applications 21.5 (2001): 34-41.

        This "masked" implementation only normalizes non-whitespace areas.

        This normalizer contains inspiration from StainTools by Peter Byfield
        (https://github.com/Peter554/StainTools).

        Args:
            threshold (float): Whitespace fraction threshold, above which
                pixels are masked and not normalized. Defaults to 0.93.
        """
        super().__init__()
        self.threshold = threshold

    def transform(
        self,
        image: np.ndarray,
        ctx_means: Optional[np.ndarray] = None,
        ctx_stds: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Normalize an H&E image.

        Args:
            img (np.ndarray): Image, RGB uint8 with dimensions W, H, C.

        Returns:
            np.ndarray: Normalized image.
        """
        image = ut.standardize_brightness(image)
        I1, I2, I3 = lab_split(image)
        mask = ((I3 + 128.) / 255. < self.threshold)[:, :, np.newaxis]
        means, stds = self._get_mean_std(image, ctx_means, ctx_stds)
        norm1 = ((I1 - means[0]) * (self.target_stds[0] / stds[0])) + self.target_means[0]
        norm2 = ((I2 - means[1]) * (self.target_stds[1] / stds[1])) + self.target_means[1]
        norm3 = ((I3 - means[2]) * (self.target_stds[2] / stds[2])) + self.target_means[2]
        return np.where(mask, merge_back(norm1, norm2, norm3), image)