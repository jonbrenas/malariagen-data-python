import json
import os

import pandas as pd
import xarray
import zarr

from malariagen_data.util import (
    DIM_ALLELE,
    DIM_PLOIDY,
    DIM_SAMPLE,
    DIM_VARIANT,
    da_from_zarr,
    init_filesystem,
    init_zarr_store,
)


class Pf7:
    def __init__(self, url, data_config=None, **kwargs):

        # setup filesystem
        self._fs, self._path = init_filesystem(url, **kwargs)
        if not data_config:
            working_dir = os.path.dirname(os.path.abspath(__file__))
            data_config = os.path.join(working_dir, "pf7_config.json")
        with open(data_config) as pf7_json_conf:
            self.CONF = json.load(pf7_json_conf)

        # setup caches
        self._cache_sample_metadata = None
        self._cache_zarr = None

    def sample_metadata(self):
        """Access sample metadata.
        Returns
        -------
        df : pandas.DataFrame
        """
        if self._cache_sample_metadata is None:
            path = os.path.join(self._path, self.CONF["metadata_path"])
            with self._fs.open(path) as f:
                self._cache_sample_metadata = pd.read_csv(f, sep="\t", na_values="")
        return self._cache_sample_metadata

    def open_zarr(self):
        if self._cache_zarr is None:
            path = os.path.join(self._path, self.CONF["zarr_path"])
            store = init_zarr_store(fs=self._fs, path=path)
            """WARNING: Metadata has not been consolidated yet. Using open for now but will eventually switch to opn_consolidated when the .zmetadata file has been created
            """
            self._cache_zarr = zarr.open(store=store)
        return self._cache_zarr

    def _variant_dataset(self, inline_array, chunks):

        # setup
        coords = dict()
        data_vars = dict()
        root = self.open_zarr()

        # variant_position
        pos_z = root["variants/POS"]
        variant_position = da_from_zarr(pos_z, inline_array=inline_array, chunks=chunks)
        coords["variant_position"] = [DIM_VARIANT], variant_position

        # variant_allele
        chrom_z = root["variants/CHROM"]
        variant_chrom = da_from_zarr(chrom_z, inline_array=inline_array, chunks=chunks)
        coords["variant_chrom"] = [DIM_VARIANT], variant_chrom

        # variant_filter_pass
        fp_z = root["variants/FILTER_PASS"]
        fp = da_from_zarr(fp_z, inline_array=inline_array, chunks=chunks)
        data_vars["variant_filter_pass"] = [DIM_VARIANT], fp

        # call arrays
        gt_z = root["calldata/GT"]
        call_genotype = da_from_zarr(gt_z, inline_array=inline_array, chunks=chunks)
        gq_z = root["calldata/GQ"]
        call_gq = da_from_zarr(gq_z, inline_array=inline_array, chunks=chunks)
        ad_z = root["calldata/AD"]
        call_ad = da_from_zarr(ad_z, inline_array=inline_array, chunks=chunks)
        data_vars["call_genotype"] = (
            [DIM_VARIANT, DIM_SAMPLE, DIM_PLOIDY],
            call_genotype,
        )
        data_vars["call_GQ"] = ([DIM_VARIANT, DIM_SAMPLE], call_gq)
        # data_vars["call_MQ"] = ([DIM_VARIANT, DIM_SAMPLE], call_mq)
        data_vars["call_AD"] = ([DIM_VARIANT, DIM_SAMPLE, DIM_ALLELE], call_ad)

        # sample arrays
        z = root["samples"]
        sample_id = da_from_zarr(z, inline_array=inline_array, chunks=chunks)
        coords["sample_id"] = [DIM_SAMPLE], sample_id

        # create a dataset
        ds = xarray.Dataset(data_vars=data_vars, coords=coords)  # , attrs=attrs)

        return ds

    def variant_calls(self, inline_array=True, chunks="native"):
        """Access variant sites, site filters and genotype calls.
        Parameters
        ----------
        inline_array : bool, optional
            Passed through to dask.array.from_array().
        chunks : str, optional
            If 'auto' let dask decide chunk size. If 'native' use native zarr chunks.
            Also can be a target size, e.g., '200 MiB'.
        Returns
        -------
        ds : xarray.Dataset
        """

        # multiple sample sets requested, need to concatenate along samples dimension
        datasets = [
            self._variant_dataset(
                inline_array=inline_array,
                chunks=chunks,
            )
        ]
        ds = xarray.concat(
            datasets,
            dim=DIM_VARIANT,
            data_vars="minimal",
            coords="minimal",
            compat="override",
            join="override",
        )

        return ds
