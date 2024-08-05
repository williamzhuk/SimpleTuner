from PIL import Image
from PIL.ImageOps import exif_transpose
from helpers.multiaspect.image import MultiaspectImage, resize_helpers
from helpers.image_manipulation.cropping import crop_handlers
from helpers.training.state_tracker import StateTracker
from helpers.training.multi_process import should_log
import logging
import os
from tqdm import tqdm
import random
import time

logger = logging.getLogger(__name__)
if should_log():
    logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL", "INFO"))
else:
    logger.setLevel("ERROR")


class TrainingSample:
    def __init__(
        self,
        image: Image.Image,
        data_backend_id: str,
        image_metadata: dict = None,
        image_path: str = None,
    ):
        """
        Initializes a new TrainingSample instance with a provided PIL.Image object and a data backend identifier.

        Args:
            image (Image.Image): A PIL Image object.
            data_backend_id (str): Identifier for the data backend used for additional operations.
            metadata (dict): Optional metadata associated with the image.
        """
        self.image = image
        self.target_size = None
        self.intermediary_size = None
        self.original_size = None
        self.data_backend_id = data_backend_id
        self.image_metadata = (
            image_metadata
            if image_metadata
            else StateTracker.get_metadata_by_filepath(image_path, data_backend_id)
        )
        if hasattr(image, "size"):
            self.original_size = self.image.size
            self.original_aspect_ratio = MultiaspectImage.calculate_image_aspect_ratio(
                self.original_size
            )
        elif image_metadata is not None:
            self.original_size = image_metadata.get("original_size")
            self.original_aspect_ratio = MultiaspectImage.calculate_image_aspect_ratio(
                self.original_size
            )
        self.current_size = self.original_size

        if not self.original_size:
            raise Exception("Original size not found in metadata.")

        # Torchvision transforms turn the pixels into a Tensor and normalize them for the VAE.
        self.transforms = MultiaspectImage.get_image_transforms()

        # Backend config details
        self.data_backend_config = StateTracker.get_data_backend_config(data_backend_id)
        self.crop_enabled = self.data_backend_config.get("crop", False)
        self.crop_style = self.data_backend_config.get("crop_style", "random")
        self.crop_aspect = self.data_backend_config.get("crop_aspect", "square")
        self.crop_aspect_buckets = self.data_backend_config.get(
            "crop_aspect_buckets", []
        )
        self.crop_coordinates = (0, 0)
        crop_handler_cls = crop_handlers.get(self.crop_style)
        if not crop_handler_cls:
            raise ValueError(f"Unknown crop style: {self.crop_style}")
        self.cropper = crop_handler_cls(image=self.image, image_metadata=image_metadata)
        self.resolution = self.data_backend_config.get("resolution")
        self.resolution_type = self.data_backend_config.get("resolution_type")
        self.target_size_calculator = resize_helpers.get(self.resolution_type)
        if self.target_size_calculator is None:
            raise ValueError(f"Unknown resolution type: {self.resolution_type}")
        self._set_resolution()
        self.target_downsample_size = self.data_backend_config.get(
            "target_downsample_size", None
        )
        self.maximum_image_size = self.data_backend_config.get(
            "maximum_image_size", None
        )
        self._image_path = image_path
        # RGB/EXIF conversions.
        self.correct_image()
        self._validate_image_metadata()

    def save_debug_image(self, path: str):
        if self.image and os.environ.get("SIMPLETUNER_DEBUG_IMAGE_PREP", "") == "true":
            self.image.save(path)
        return self

    @staticmethod
    def from_image_path(image_path: str, data_backend_id: str):
        """
        Create a new TrainingSample instance from an image path.

        Args:
            image_path (str): The path to the image.
            data_backend_id (str): Identifier for the data backend used for additional operations.

        Returns:
            TrainingSample: A new TrainingSample instance.
        """
        data_backend = StateTracker.get_data_backend(data_backend_id)
        image = data_backend["metadata_backend"].read_image(image_path)
        return TrainingSample(image, data_backend_id, image_path=image_path)

    def _validate_image_metadata(self) -> bool:
        """
        Determine whether all required keys exist for prepare() to skip calculations.
        This is useful because randomised aspect buckets must be preserved across runs to avoid mismatched tensor dimensions.

        Returns:
            bool: True if the metadata is valid, False otherwise.
        """
        required_keys = [
            "original_size",
            "target_size",
            "intermediary_size",
            "crop_coordinates",
            "aspect_ratio",
        ]
        if type(self.image_metadata) is not dict:
            self.valid_metadata = False
        else:
            self.valid_metadata = all(
                key in self.image_metadata for key in required_keys
            )
        if self.valid_metadata:
            self.original_size = self.image_metadata["original_size"]
            self.target_size = self.image_metadata["target_size"]
            self.intermediary_size = self.image_metadata["intermediary_size"]
            self.crop_coordinates = self.image_metadata["crop_coordinates"]
            self.aspect_ratio = self.image_metadata["aspect_ratio"]

        self.original_aspect_ratio = MultiaspectImage.calculate_image_aspect_ratio(
            self.original_size
        )

        if not self.valid_metadata and hasattr(self.image, "size"):
            self.original_size = self.image.size

        return self.valid_metadata

    def _set_resolution(self):
        if self.resolution_type == "pixel":
            self.target_area = self.resolution
            # Store the pixel value, eg. 1024
            self.pixel_resolution = int(self.resolution)
            # Store the megapixel value, eg. 1.0
            self.megapixel_resolution = self.resolution / 1e3
        elif self.resolution_type == "area":
            self.target_area = self.resolution * 1e6  # Convert megapixels to pixels
            # Store the pixel value, eg. 1024
            self.pixel_resolution = int(
                MultiaspectImage._round_to_nearest_multiple(self.resolution * 1e3)
            )
            # Store the megapixel value, eg. 1.0
            self.megapixel_resolution = self.resolution
        else:
            raise Exception(f"Unknown resolution type: {self.resolution_type}")

    def _trim_aspect_bucket_list(self):
        """
        Momentarily return a temporarily list of pruned buckets that'll work for this image.
        An aspect bucket will "work" if the image must be upscaled less than 20% to fit into it.

        Returns:
            list[float]: The list of available aspect buckets
        """
        available_buckets = []
        for bucket in self.crop_aspect_buckets:
            # We want to ensure we don't upscale images beyond about 20% of their original size.
            # If any of the aspect buckets will result in that, we'll ignore it.
            if type(bucket) is dict:
                aspect = bucket["aspect_ratio"]
            elif type(bucket) is float:
                aspect = bucket
            else:
                raise ValueError(
                    "Aspect buckets must be a list of floats or dictionaries."
                )
            # Calculate new size
            target_size, intermediary_size, aspect_ratio = self.target_size_calculator(
                aspect, self.resolution, self.original_size
            )
            # Check the size vs a 20% threshold
            if (
                target_size[0] * 1.2 < self.original_size[0]
                and target_size[1] * 1.2 < self.original_size[1]
            ):
                available_buckets.append(aspect)
        return available_buckets

    def _select_random_aspect(self):
        """
        This method returns a random aspect bucket from a list of configured, allowed values.

        Returns:
            float: The selected aspect ratio.
        """
        if not self.crop_aspect_buckets:
            raise ValueError(
                "Aspect buckets are not defined in the data backend config."
            )
        if self.valid_metadata:
            self.aspect_ratio = self.image_metadata["aspect_ratio"]
            return self.aspect_ratio
        if (
            len(self.crop_aspect_buckets) > 0
            and type(self.crop_aspect_buckets[0]) is dict
        ):
            has_portrait_buckets = any(
                bucket["aspect"] < 1.0 for bucket in self.crop_aspect_buckets
            )
            has_landscape_buckets = any(
                bucket["aspect"] > 1.0 for bucket in self.crop_aspect_buckets
            )
            # our aspect ratio is w / h
            # so portrait is < 1.0 and landscape is > 1.0
            if not has_portrait_buckets or not has_landscape_buckets:
                return 1.0
            if (
                not has_portrait_buckets
                and self.aspect_ratio < 1.0
                or not has_landscape_buckets
                and self.aspect_ratio > 1.0
            ):
                logger.warning(
                    f"No {'portrait' if self.aspect_ratio < 1.0 else 'landscape'} aspect buckets found, defaulting to 1.0 square crop. Define a {'portrait' if self.aspect_ratio < 1.0 else 'landscape'} aspect bucket to avoid this warning"
                )
                return 1.0
            total_weight = sum(bucket["weight"] for bucket in self.crop_aspect_buckets)
            if total_weight != 1.0:
                raise ValueError("The weights of aspect buckets must add up to 1.")

            aspects = [bucket["aspect"] for bucket in self.crop_aspect_buckets]
            weights = [bucket["weight"] for bucket in self.crop_aspect_buckets]

            selected_aspect = random.choices(aspects, weights)[0]
        elif (
            len(self.crop_aspect_buckets) > 0
            and type(self.crop_aspect_buckets[0]) is float
        ):
            has_landscape_buckets = any(
                bucket > 1.0 for bucket in self.crop_aspect_buckets
            )
            has_portrait_buckets = any(
                bucket < 1.0 for bucket in self.crop_aspect_buckets
            )
            if not has_portrait_buckets or not has_landscape_buckets:
                return 1.0
            if (
                not has_portrait_buckets
                and self.aspect_ratio < 1.0
                or not has_landscape_buckets
                and self.aspect_ratio > 1.0
            ):
                logger.warning(
                    f"No {'portrait' if self.aspect_ratio < 1.0 else 'landscape'} aspect buckets found, defaulting to 1.0 square crop. Define a {'portrait' if self.aspect_ratio < 1.0 else 'landscape'} aspect bucket to avoid this warning"
                )
                return 1.0
            # filter to portrait or landscape buckets, depending on our aspect ratio
            available_aspects = self._trim_aspect_bucket_list()
            if len(available_aspects) == 0:
                selected_aspect = 1.0
                if should_log():
                    tqdm.write(
                        "[WARNING] Image dimensions do not fit into the configured aspect buckets. Using square crop."
                    )
            else:
                selected_aspect = random.choice(available_aspects)
        else:
            raise ValueError(
                "Aspect buckets must be a list of floats or dictionaries."
                " If using a dictionary, it is expected to be in the format {'aspect': 1.0, 'weight': 0.5}."
                " To provide multiple aspect ratios, use a list of dictionaries: [{'aspect': 1.0, 'weight': 0.5}, {'aspect': 1.5, 'weight': 0.5}]."
            )

        return selected_aspect

    def prepare(self, return_tensor: bool = False):
        """
        Perform initial image preparations such as converting to RGB and applying EXIF transformations.

        Args:
            image (Image.Image): The image to prepare.

        Returns: tuple
            - image data (PIL.Image)
            - crop_coordinates (tuple)
            - aspect_ratio (float)
        """
        self.save_debug_image(f"images/{time.time()}-0-original.png")
        self.crop()
        self.save_debug_image(f"images/{time.time()}-1-cropped.png")
        if not self.crop_enabled:
            self.save_debug_image(f"images/{time.time()}-1b-nocrop-resize.png")
            self.resize()

        image = self.image
        if return_tensor:
            # Return normalised tensor.
            image = self.transforms(image)
        webhook_handler = StateTracker.get_webhook_handler()
        prepared_sample = PreparedSample(
            image=image,
            original_size=self.original_size,
            crop_coordinates=self.crop_coordinates,
            aspect_ratio=self.aspect_ratio,
            image_metadata=self.image_metadata,
            target_size=self.target_size,
            intermediary_size=self.intermediary_size,
        )
        if webhook_handler:
            webhook_handler.send(
                message=f"Debug info for prepared sample, {str(prepared_sample)}",
                images=[self.image] if self.image else None,
                message_level="debug",
            )
        return prepared_sample

    def area(self) -> int:
        """
        Calculate the area of the image.

        Returns:
            int: The area of the image.
        """
        if self.image is not None:
            return self.image.size[0] * self.image.size[1]
        if self.original_size:
            return self.original_size[0] * self.original_size[1]

    def _should_resize_before_crop(self) -> bool:
        """
        If the options to do so are enabled, or, the image require it; we will resize before cropping.

        Returns:
            bool: True if the image should be resized before cropping, False otherwise.
        """
        if (
            not self.crop_enabled
            or not self.maximum_image_size
            or not self.target_downsample_size
        ):
            return False
        if self.data_backend_config.get("resolution_type") == "pixel":
            return (
                self.current_size[0] > self.pixel_resolution
                or self.current_size[1] > self.pixel_resolution
            ) or (
                self.current_size[0] < self.pixel_resolution
                or self.current_size[1] < self.pixel_resolution
            )
        elif self.data_backend_config.get("resolution_type") == "area":
            should_resize = (
                self.area() > self.target_area
                or self.area() < self.target_area
                or self.current_size[0] < self.target_size[0]
                or self.current_size[1] < self.target_size[1]
            )
            logger.debug(f"Should resize? {should_resize}")
            return should_resize
        else:
            raise ValueError(
                f"Unknown resolution type: {self.data_backend_config.get('resolution_type')}"
            )

    def _calculate_target_downsample_size(self):
        """
        When cropping images, it is optional to disturb them with a resize before the crop.
        This is desirable when a large image is being cropped to a small size, as it will preserve scene details and maintain aspect ratio.

        Returns:
            tuple: The target downsample size as (width, height).
        """
        # We'll run the target size calculator logic without updating any of the object attributes.
        # This will prevent contamination of the final values that the image will represent.
        _, calculated_intermediary_size, _ = self.target_size_calculator(
            self.original_aspect_ratio, self.target_downsample_size, self.original_size
        )
        # The calculated_intermediary_size's purpose is to resize to this value before cropping to target_size.
        # If the intermediary size is smaller than target_size on either edge, the cropping will result in black bars.
        # We have to calculate the scale factor and adjust the image edges proportionally to avoid squishing it.
        if calculated_intermediary_size[0] < self.target_size[0]:
            scale_factor = self.target_size[0] / calculated_intermediary_size[0]
            calculated_intermediary_size = (
                self.target_size[0],
                int(calculated_intermediary_size[1] * scale_factor),
            )
        elif calculated_intermediary_size[1] < self.target_size[1]:
            scale_factor = self.target_size[1] / calculated_intermediary_size[1]
            calculated_intermediary_size = (
                int(calculated_intermediary_size[0] * scale_factor),
                self.target_size[1],
            )

        return calculated_intermediary_size

    def _downsample_before_crop(self):
        """
        Downsample the image before cropping, to preserve scene details and maintain aspect ratio.

        Returns:
            TrainingSample: The current TrainingSample instance.
        """
        if self._should_resize_before_crop():
            target_downsample_size = self._calculate_target_downsample_size()
            logger.debug(f"resizing to {target_downsample_size}")
            self.resize(target_downsample_size)
        return self

    def correct_intermediary_square_size(self):
        """
        When an intermediary size is calculated, we don't adjust it to be divisible by 8 or 64.
        However, the aspect ratio 1.0 needs special consideration for our base resolutions 512, 768, and 1024, because they typically result in 500x500, 750x750, and 1000x1000 images.

        Returns:
            TrainingSample: The current TrainingSample instance.
        """
        if (
            self.aspect_ratio == 1.0
            and self.intermediary_size[0] < self.pixel_resolution
        ):
            self.intermediary_size = (
                self.pixel_resolution,
                self.pixel_resolution,
            )
            self.crop_coordinates = (0, 0)
        return self

    def calculate_target_size(self):
        """
        This method will populate the values for self.{target_size,intermediary_size,aspect_ratio} based on the image's original size and the data backend configuration.

        Returns:
            tuple:
                - The target size as (width, height).
                - The intermediary size as (width, height).
                - The aspect ratio of the target size. This will likely be different from the original aspect ratio.
        """
        self.aspect_ratio = MultiaspectImage.calculate_image_aspect_ratio(
            self.original_size
        )
        if self.crop_enabled:
            if self.crop_aspect == "square":
                self.target_size = (self.pixel_resolution, self.pixel_resolution)
                _, self.intermediary_size, _ = self.target_size_calculator(
                    self.aspect_ratio, self.resolution, self.original_size
                )
                self.aspect_ratio = 1.0
                self.correct_intermediary_square_size()
                square_crop_metadata = (
                    self.target_size,
                    self.intermediary_size,
                    self.aspect_ratio,
                )
                logger.debug(f"Square crop metadata: {square_crop_metadata}")
                return square_crop_metadata
        if self.crop_enabled and self.crop_aspect == "random":
            # Grab a random aspect ratio from a list.
            self.aspect_ratio = self._select_random_aspect()
        self.target_size, calculated_intermediary_size, self.aspect_ratio = (
            self.target_size_calculator(
                self.aspect_ratio, self.resolution, self.original_size
            )
        )
        if self.crop_aspect != "random" or not self.valid_metadata:
            self.intermediary_size = calculated_intermediary_size
        self.aspect_ratio = MultiaspectImage.calculate_image_aspect_ratio(
            self.target_size
        )
        self.correct_intermediary_square_size()
        if self.aspect_ratio == 1.0:
            self.target_size = (self.pixel_resolution, self.pixel_resolution)

        return (
            self.target_size,
            (int(self.intermediary_size[0]), int(self.intermediary_size[1])),
            self.aspect_ratio,
        )

    def correct_image(self):
        """
        Apply a series of transformations to the image to "correct" it, such as EXIF rotation and conversion to RGB.

        Returns:
            TrainingSample: The current TrainingSample instance.
        """
        if self.image:
            # Convert image to RGB to remove any alpha channel and apply EXIF data transformations
            self.image = self.image.convert("RGB")
            self.image = exif_transpose(self.image)
        return self

    def crop(self):
        """
        Crop the image using the detected crop handler class.
        If cropping is not enabled, we do nothing.

        Returns:
            TrainingSample: The current TrainingSample instance.
        """
        if not self.crop_enabled:
            return self
        # Too-big of an image, resize before we crop.
        self.calculate_target_size()
        self._downsample_before_crop()
        self.save_debug_image(f"images/{time.time()}-0.5-downsampled.png")
        if self.image is not None:
            logger.debug(f"setting image: {self.image.size}")
            self.cropper.set_image(self.image)
        logger.debug(f"Cropper size updating to {self.current_size}")
        self.cropper.set_intermediary_size(self.current_size[0], self.current_size[1])
        self.image, self.crop_coordinates = self.cropper.crop(
            self.target_size[0], self.target_size[1]
        )
        self.current_size = self.target_size
        logger.debug(
            f"Cropped to {self.image.size if self.image is not None else self.current_size} via crop coordinates {self.crop_coordinates} {'resulting in current_size of' if self.image is not None else ''} {self.current_size if self.image is not None else ''}"
        )
        return self

    def resize(self, size: tuple = None):
        """
        Resize the image to a new size. If one is not provided, we will use the precalculated self.target_size

        Args:
            (optional) target_size (tuple): The target size as (width, height).
        Returns:
            TrainingSample: The current TrainingSample instance.
        """
        current_size = self.image.size if self.image is not None else self.original_size
        if size is None:
            if not self.valid_metadata:
                self.target_size, self.intermediary_size, self.target_aspect_ratio = (
                    self.calculate_target_size()
                )
            size = self.target_size
            if self.target_size != self.intermediary_size:
                logger.debug(
                    f"we have to crop because target size {self.target_size} != intermediary size {self.intermediary_size}"
                )
                # Now we can resize the image to the intermediary size.
                if self.image is not None:
                    self.image = self.image.resize(
                        self.intermediary_size, Image.Resampling.LANCZOS
                    )
                self.current_size = self.intermediary_size
                if self.image is not None and self.cropper:
                    self.cropper.set_image(self.image)
                self.cropper.set_intermediary_size(
                    self.intermediary_size[0], self.intermediary_size[1]
                )
                self.image, self.crop_coordinates = self.cropper.crop(
                    self.target_size[0], self.target_size[1]
                )
                logger.debug(f"crop coordinates: {self.crop_coordinates}")
                return self

        if self.image and hasattr(self.image, "resize"):
            self.image = self.image.resize(size, Image.Resampling.LANCZOS)
            self.aspect_ratio = MultiaspectImage.calculate_image_aspect_ratio(
                self.image.size
            )
        self.current_size = size
        logger.debug(
            f"Resized to {self.current_size} (aspect ratio: {self.aspect_ratio})"
        )
        return self

    def get_image(self):
        """
        Returns the current state of the image.
        If using the `parquet` metadata backend, this value may be None during the initial aspect bucketing phase.

        Returns:
            Image.Image: The current image.
        """
        return self.image

    def get_conditioning_image(self):
        """
        Fetch a conditioning image, eg. a canny edge map for ControlNet training.
        Currently, this example is not implemented or used.

        Returns:
            None
        """
        if not StateTracker.get_args().controlnet:
            return None
        conditioning_dataset = StateTracker.get_conditioning_dataset(
            data_backend_id=self.data_backend_id
        )
        raise NotImplementedError("Conditioning images are not yet implemented.")

    def cache_path(self):
        """
        Given an image path, manipulate the prefix and suffix to return its counterpart cache path.
        The image extension will be stripped and replaced with the appropriate value (.pt).
        If the instance_data_dir is found in the path, it will be replaced with the cache_dir.

        Returns:
            str: The cache path for the image.
        """
        vae_cache = StateTracker.get_data_backend(self.data_backend_id)["vaecache"]

        return vae_cache.image_path_to_vae_path.get(self._image_path, None)

    def image_path(self, basename_only=False):
        """
        Returns the absolute or basename path for the current training sample.

        Args:
            basename_only (bool): Whether to return the basename only.
        Returns:
            str: The image path
        """
        if basename_only:
            return os.path.basename(self._image_path)
        return self._image_path


class PreparedSample:
    def __init__(
        self,
        image: Image.Image,
        image_metadata: dict,
        original_size: tuple,
        intermediary_size: tuple,
        target_size: tuple,
        aspect_ratio: float,
        crop_coordinates: tuple,
    ):
        """
        Initializes a new PreparedSample instance with a provided PIL.Image object and optional metadata.

        Args:
        image (Image.Image): A PIL Image object.
        metadata (dict): Optional metadata associated with the image.
        """
        self.image = image
        self.image_metadata = image_metadata if image_metadata else {}
        self.original_size = original_size
        self.intermediary_size = intermediary_size
        self.target_size = target_size
        if image is not None and hasattr(image, "size") and type(image.size) is tuple:
            self.aspect_ratio = MultiaspectImage.calculate_image_aspect_ratio(
                image.size[0] / image.size[1]
            )
        else:
            self.aspect_ratio = aspect_ratio
        self.crop_coordinates = crop_coordinates

    def __str__(self):
        return f"PreparedSample(image={self.image}, original_size={self.original_size}, intermediary_size={self.intermediary_size}, target_size={self.target_size}, aspect_ratio={self.aspect_ratio}, crop_coordinates={self.crop_coordinates})"

    def to_dict(self):
        return {
            "image": self.image,
            "original_size": self.original_size,
            "intermediary_size": self.intermediary_size,
            "target_size": self.target_size,
            "aspect_ratio": self.aspect_ratio,
            "crop_coordinates": self.crop_coordinates,
        }
