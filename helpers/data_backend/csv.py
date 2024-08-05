import fnmatch
import io
from datetime import datetime
from urllib.request import url2pathname

import pandas as pd
import requests
from PIL import Image

from helpers.data_backend.base import BaseDataBackend
from helpers.image_manipulation.load import load_image
from helpers.training.multi_process import should_log
from pathlib import Path
from io import BytesIO
import os
import logging
import torch
from typing import Any, Union, Optional, BinaryIO

logger = logging.getLogger("CSVDataBackend")
if should_log():
    logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL", "INFO"))
else:
    logger.setLevel("ERROR")


def url_to_filename(url: str) -> str:
    return url.split("/")[-1]


def shorten_and_clean_filename(filename: str, no_op: bool):
    if no_op:
        return filename
    filename = filename.replace("%20", "-").replace(" ", "-")
    if len(filename) > 250:
        filename = filename[:120] + "---" + filename[126:]
    return filename


def html_to_file_loc(parent_directory: Path, url: str, shorten_filenames: bool) -> str:
    filename = url_to_filename(url)
    cached_loc = str(
        parent_directory.joinpath(
            shorten_and_clean_filename(filename, no_op=shorten_filenames)
        )
    )
    return cached_loc


class CSVDataBackend(BaseDataBackend):
    def __init__(
        self,
        accelerator,
        id: str,
        csv_file: Path,
        compress_cache: bool = False,
        image_url_col: str = "url",
        caption_column: str = "caption",
        url_column: str = "url",
        image_cache_loc: Optional[str] = None,
        shorten_filenames: bool = False,
    ):
        self.id = id
        self.type = "csv"
        self.compress_cache = compress_cache
        self.shorten_filenames = shorten_filenames
        self.csv_file = csv_file
        self.accelerator = accelerator
        self.image_url_col = image_url_col
        self.df = pd.read_csv(csv_file, index_col=image_url_col)
        self.df = self.df.groupby(level=0).last()  # deduplicate by index (image loc)
        self.caption_column = caption_column
        self.url_column = url_column
        self.image_cache_loc = (
            Path(image_cache_loc) if image_cache_loc is not None else None
        )

    def read(self, location, as_byteIO: bool = False):
        """Read and return the content of the file."""
        if isinstance(location, Path):
            location = str(location.resolve())
        if location.startswith("http"):
            if self.image_cache_loc is not None:
                # check for cache
                cached_loc = html_to_file_loc(
                    self.image_cache_loc,
                    location,
                    shorten_filenames=self.shorten_filenames,
                )
                if os.path.exists(cached_loc):
                    # found cache
                    location = cached_loc
                else:
                    # actually go to website
                    data = requests.get(location, stream=True).raw.data
                    with open(cached_loc, "wb") as f:
                        f.write(data)
            else:
                data = requests.get(location, stream=True).raw.data
        if not location.startswith("http"):
            # read from local file
            with open(location, "rb") as file:
                data = file.read()
        if not as_byteIO:
            return data
        return BytesIO(data)

    def write(self, filepath: Union[str, Path], data: Any) -> None:
        """Write the provided data to the specified filepath."""
        if isinstance(filepath, str):
            assert not filepath.startswith(
                "http"
            ), f"writing to {filepath} is not allowed as it has http in it"
            filepath = Path(filepath)
        # Not a huge fan of auto-shortening filenames, as we hash things for that in other cases.
        # However, this is copied in from the original Arcade-AI CSV backend implementation for compatibility.
        filepath = filepath.parent.joinpath(
            shorten_and_clean_filename(filepath.name, no_op=self.shorten_filenames)
        )
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as file:
            # Check if data is a Tensor, and if so, save it appropriately
            if isinstance(data, torch.Tensor):
                # logger.debug(f"Writing a torch file to disk.")
                return self.torch_save(data, file)
            if isinstance(data, str):
                # logger.debug(f"Writing a string to disk as {filepath}: {data}")
                data = data.encode("utf-8")
            else:
                logger.debug(
                    f"Received an unknown data type to write to disk. Doing our best: {type(data)}"
                )
            file.write(data)

    def delete(self, filepath):
        """Delete the specified file."""
        if filepath in self.df.index:
            self.df.drop(filepath, inplace=True)
            # self.save_state()
        if os.path.exists(filepath):
            logger.debug(f"Deleting file: {filepath}")
            os.remove(filepath)
        # Validate that we deleted it correctly.
        if self.exists(filepath) or filepath in self.df.index:
            raise Exception(f"Failed to delete {filepath}")

    def exists(self, filepath):
        """Check if the file exists."""
        if isinstance(filepath, Path):
            filepath = str(filepath.resolve())
        return filepath in self.df.index or os.path.exists(filepath)

    def open_file(self, filepath, mode):
        """Open the file in the specified mode."""
        return open(filepath, mode)

    def list_files(self, str_pattern: str, instance_data_dir: str = None) -> tuple:
        """
        List all files matching the pattern.
        Creates Path objects of each file found.
        """
        # print frame contents
        logger.debug(
            f"CSVDataBackend.list_files: str_pattern={str_pattern}, instance_data_dir={instance_data_dir}"
        )
        if instance_data_dir is None:
            filtered_paths = set(self.df.index)
            filtered_ids = set(filtered_paths)
        else:
            filtered_ids = set(
                filter(lambda id: fnmatch.fnmatch(id, str_pattern), list(self.df.index))
            )
            filtered_paths = set(
                filter(lambda id: "http" not in id and os.path.exists(id), filtered_ids)
            )
        # Group files by their parent directory
        path_dict = {}
        for path in filtered_paths:
            if hasattr(path, "parent"):
                parent = str(path.parent)
                if parent not in path_dict:
                    path_dict[parent] = []
                path_dict[parent].append(str(path.absolute()))
            else:
                if "/" not in path_dict:
                    path_dict["/"] = []
                if os.path.splitext(str(path))[1] not in [".json", ".csv", ".parquet"]:
                    path_dict["/"].append(str(path))

        results = [(subdir, [], files) for subdir, files in path_dict.items()]
        results += [("", [], filtered_ids - filtered_paths)]
        return results

    def read_image(self, filepath: str, delete_problematic_images: bool = False):
        # Remove embedded null byte:
        if isinstance(filepath, str):
            filepath = filepath.replace("\x00", "")
        try:
            image_data = self.read(filepath, as_byteIO=True)
            image = load_image(image_data)
            return image
        except Exception as e:
            import traceback

            logger.error(
                f"Encountered error opening image {filepath}: {e}, traceback: {traceback.format_exc()}"
            )
            if delete_problematic_images:
                logger.error(
                    "Deleting image, because --delete_problematic_images is provided."
                )
                self.delete(filepath)
            else:
                exit(1)
                raise e

    def read_image_batch(
        self, filepaths: list, delete_problematic_images: bool = False
    ) -> list:
        """Read a batch of images from the specified filepaths."""
        if type(filepaths) != list:
            raise ValueError(
                f"read_image_batch must be given a list of image filepaths. we received: {filepaths}"
            )
        output_images = []
        available_keys = []
        for filepath in filepaths:
            try:
                image_data = self.read_image(filepath, delete_problematic_images)
                if image_data is None:
                    logger.warning(f"Unable to load image '{filepath}', skipping.")
                    continue
                output_images.append(image_data)
                available_keys.append(filepath)
            except Exception as e:
                if delete_problematic_images:
                    logger.error(
                        f"Deleting image '{filepath}', because --delete_problematic_images is provided. Error: {e}"
                    )
                else:
                    logger.warning(
                        f"A problematic image {filepath} is detected, but we are not allowed to remove it, because --delete_problematic_image is not provided."
                        f" Please correct this manually. Error: {e}"
                    )
        return (available_keys, output_images)

    def create_directory(self, directory_path):
        if os.path.exists(directory_path):
            return
        logger.debug(f"Creating directory: {directory_path}")
        os.makedirs(directory_path, exist_ok=True)

    def torch_load(self, filename):
        """
        Load a torch tensor from a file.
        """

        stored_tensor = self.read(filename, as_byteIO=True)

        if self.compress_cache:
            try:
                stored_tensor = self._decompress_torch(stored_tensor)
            except Exception as e:
                logger.error(
                    f"Failed to decompress torch file, falling back to passthrough: {e}"
                )
        if hasattr(stored_tensor, "seek"):
            stored_tensor.seek(0)
        try:
            loaded_tensor = torch.load(stored_tensor, map_location="cpu")
        except Exception as e:
            logger.error(f"Failed to load corrupt torch file '{filename}': {e}")
            if "invalid load key" in str(e):
                self.delete(filename)
            raise e
        return loaded_tensor

    def torch_save(self, data, location: Union[str, Path, BytesIO]):
        """
        Save a torch tensor to a file.
        """
        if isinstance(location, str) or isinstance(location, Path):
            if location not in self.df.index:
                self.df.loc[location] = pd.Series()
            location = self.open_file(location, "wb")

        if self.compress_cache:
            compressed_data = self._compress_torch(data)
            location.write(compressed_data)
        else:
            torch.save(data, location)
        location.close()

    def write_batch(self, filepaths: list, data_list: list) -> None:
        """Write a batch of data to the specified filepaths."""
        for filepath, data in zip(filepaths, data_list):
            self.write(filepath, data)

    def save_state(self):
        self.df.to_csv(self.csv_file, index_label=self.image_url_col)

    def get_caption(self, image_path: str) -> str:
        if self.caption_column is None:
            raise ValueError("Cannot retrieve caption from csv, as one is not set.")
        return self.df.loc[image_path, self.caption_column]
