import warnings
from abc import abstractmethod
from collections import Counter
from itertools import cycle
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

import allel
import bokeh.layouts
import bokeh.models
import bokeh.palettes
import bokeh.plotting
import dask.array as da
import igv_notebook
import numba
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import xarray as xr
from numpydoc_decorator import doc

from . import veff
from .anoph import (
    aim_params,
    base_params,
    dash_params,
    frq_params,
    fst_params,
    g123_params,
    gplt_params,
    h12_params,
    hapclust_params,
    hapnet_params,
    het_params,
    ihs_params,
    map_params,
    pca_params,
    plotly_params,
)
from .anoph.aim_data import AnophelesAimData
from .anoph.base import AnophelesBase
from .anoph.base_params import DEFAULT
from .anoph.genome_features import AnophelesGenomeFeaturesData
from .anoph.genome_sequence import AnophelesGenomeSequenceData
from .anoph.hap_data import AnophelesHapData, hap_params
from .anoph.sample_metadata import AnophelesSampleMetadata
from .anoph.snp_data import AnophelesSnpData
from .mjn import median_joining_network, mjn_graph
from .util import (
    CacheMiss,
    Region,
    check_types,
    jackknife_ci,
    jitter,
    locate_region,
    parse_single_region,
    plotly_discrete_legend,
)

AA_CHANGE_QUERY = (
    "effect in ['NON_SYNONYMOUS_CODING', 'START_LOST', 'STOP_LOST', 'STOP_GAINED']"
)

# N.B., we are in the process of breaking up the AnophelesDataResource
# class into multiple parent classes like AnophelesGenomeSequenceData
# and AnophelesBase. This is work in progress, and further PRs are
# expected to factor out functions defined here in to separate classes.
# For more information, see:
#
# https://github.com/malariagen/malariagen-data-python/issues/366
#
# N.B., we are making use of multiple inheritance here, using co-operative
# classes. Because of the way that multiple inheritance works in Python,
# it is important that these parent classes are provided in a particular
# order. Otherwise the linearization of parent classes will fail. For
# more information about superclass linearization and method resolution
# order in Python, the following links may be useful.
#
# https://en.wikipedia.org/wiki/C3_linearization
# https://rhettinger.wordpress.com/2011/05/26/super-considered-super/


# work around pycharm failing to recognise that doc() is callable
# noinspection PyCallingNonCallable
class AnophelesDataResource(
    AnophelesAimData,
    AnophelesHapData,
    AnophelesSnpData,
    AnophelesSampleMetadata,
    AnophelesGenomeFeaturesData,
    AnophelesGenomeSequenceData,
    AnophelesBase,
):
    """Anopheles data resources."""

    def __init__(
        self,
        url,
        config_path,
        cohorts_analysis: Optional[str],
        aim_analysis: Optional[str],
        aim_metadata_dtype: Optional[Mapping[str, Any]],
        aim_ids: Optional[aim_params.aim_ids],
        aim_palettes: Optional[aim_params.aim_palettes],
        site_filters_analysis: Optional[str],
        default_site_mask: Optional[str],
        default_phasing_analysis: Optional[str],
        bokeh_output_notebook: bool,
        results_cache: Optional[str],
        log,
        debug,
        show_progress,
        check_location,
        pre,
        gcs_url: str,
        major_version_number: int,
        major_version_path: str,
        gff_gene_type: str,
        gff_default_attributes: Tuple[str, ...],
        storage_options: Mapping,  # used by fsspec via init_filesystem(url, **kwargs)
    ):
        super().__init__(
            url=url,
            config_path=config_path,
            bokeh_output_notebook=bokeh_output_notebook,
            log=log,
            debug=debug,
            show_progress=show_progress,
            check_location=check_location,
            pre=pre,
            gcs_url=gcs_url,
            major_version_number=major_version_number,
            major_version_path=major_version_path,
            storage_options=storage_options,
            gff_gene_type=gff_gene_type,
            gff_default_attributes=gff_default_attributes,
            cohorts_analysis=cohorts_analysis,
            aim_analysis=aim_analysis,
            aim_metadata_dtype=aim_metadata_dtype,
            aim_ids=aim_ids,
            aim_palettes=aim_palettes,
            site_filters_analysis=site_filters_analysis,
            default_site_mask=default_site_mask,
            default_phasing_analysis=default_phasing_analysis,
            results_cache=results_cache,
        )

        # set up caches
        # TODO review type annotations here, maybe can tighten
        self._cache_annotator = None

    @property
    @abstractmethod
    def _pca_results_cache_name(self):
        raise NotImplementedError("Must override _pca_results_cache_name")

    @property
    @abstractmethod
    def _fst_gwss_results_cache_name(self):
        raise NotImplementedError("Must override _fst_gwss_results_cache_name")

    @property
    @abstractmethod
    def _h12_calibration_cache_name(self):
        raise NotImplementedError("Must override _h12_calibration_cache_name")

    @property
    @abstractmethod
    def _g123_calibration_cache_name(self):
        raise NotImplementedError("Must override _g123_calibration_cache_name")

    @property
    @abstractmethod
    def _h12_gwss_cache_name(self):
        raise NotImplementedError("Must override _h12_gwss_cache_name")

    @property
    @abstractmethod
    def _g123_gwss_cache_name(self):
        raise NotImplementedError("Must override _g123_gwss_cache_name")

    @property
    @abstractmethod
    def _h1x_gwss_cache_name(self):
        raise NotImplementedError("Must override _h1x_gwss_cache_name")

    @property
    @abstractmethod
    def _ihs_gwss_cache_name(self):
        raise NotImplementedError("Must override _ihs_gwss_cache_name")

    @abstractmethod
    def _transcript_to_gene_name(self, transcript):
        # children may have different manual overrides.
        raise NotImplementedError("Must override _transcript_to_gene_name")

    @abstractmethod
    def _view_alignments_add_site_filters_tracks(
        self, *, contig, visibility_window, tracks
    ):
        # default implementation, do nothing
        raise NotImplementedError(
            "Must override _view_alignments_add_site_filters_tracks"
        )

    @check_types
    @doc(
        summary="""
            Group samples by taxon, area (space) and period (time), then compute
            SNP allele frequencies.
        """,
        returns="""
            The resulting dataset contains data has dimensions "cohorts" and
            "variants". Variables prefixed with "cohort" are 1-dimensional
            arrays with data about the cohorts, such as the area, period, taxon
            and cohort size. Variables prefixed with "variant" are
            1-dimensional arrays with data about the variants, such as the
            contig, position, reference and alternate alleles. Variables
            prefixed with "event" are 2-dimensional arrays with the allele
            counts and frequency calculations.
        """,
    )
    def snp_allele_frequencies_advanced(
        self,
        transcript: base_params.transcript,
        area_by: frq_params.area_by,
        period_by: frq_params.period_by,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        min_cohort_size: base_params.min_cohort_size = 10,
        drop_invariant: frq_params.drop_invariant = True,
        variant_query: Optional[frq_params.variant_query] = None,
        site_mask: Optional[base_params.site_mask] = None,
        nobs_mode: frq_params.nobs_mode = frq_params.nobs_mode_default,
        ci_method: Optional[frq_params.ci_method] = frq_params.ci_method_default,
    ) -> xr.Dataset:
        debug = self._log.debug

        debug("check parameters")
        self._check_param_min_cohort_size(min_cohort_size)

        debug("load sample metadata")
        df_samples = self.sample_metadata(
            sample_sets=sample_sets, sample_query=sample_query
        )

        debug("access SNP calls")
        ds_snps = self.snp_calls(
            region=transcript,
            sample_sets=sample_sets,
            sample_query=sample_query,
            site_mask=site_mask,
        )

        debug("access genotypes")
        gt = ds_snps["call_genotype"].data

        debug("prepare sample metadata for cohort grouping")
        df_samples = self._prep_samples_for_cohort_grouping(
            df_samples=df_samples,
            area_by=area_by,
            period_by=period_by,
        )

        debug("group samples to make cohorts")
        group_samples_by_cohort = df_samples.groupby(["taxon", "area", "period"])

        debug("build cohorts dataframe")
        df_cohorts = self._build_cohorts_from_sample_grouping(
            group_samples_by_cohort, min_cohort_size
        )

        debug("bring genotypes into memory")
        with self._dask_progress(desc="Load SNP genotypes"):
            gt = gt.compute()

        debug("set up variant variables")
        contigs = ds_snps.attrs["contigs"]
        variant_contig = np.repeat(
            [contigs[i] for i in ds_snps["variant_contig"].values], 3
        )
        variant_position = np.repeat(ds_snps["variant_position"].values, 3)
        alleles = ds_snps["variant_allele"].values
        variant_ref_allele = np.repeat(alleles[:, 0], 3)
        variant_alt_allele = alleles[:, 1:].flatten()
        variant_pass = dict()
        for site_mask in self.site_mask_ids:
            variant_pass[site_mask] = np.repeat(
                ds_snps[f"variant_filter_pass_{site_mask}"].values, 3
            )

        debug("setup main event variables")
        n_variants, n_cohorts = len(variant_position), len(df_cohorts)
        count = np.zeros((n_variants, n_cohorts), dtype=int)
        nobs = np.zeros((n_variants, n_cohorts), dtype=int)

        debug("build event count and nobs for each cohort")
        cohorts_iterator = self._progress(
            enumerate(df_cohorts.itertuples()),
            total=len(df_cohorts),
            desc="Compute SNP allele frequencies",
        )
        for cohort_index, cohort in cohorts_iterator:
            cohort_key = cohort.taxon, cohort.area, cohort.period
            sample_indices = group_samples_by_cohort.indices[cohort_key]

            cohort_ac, cohort_an = self._cohort_alt_allele_counts_melt(
                gt, sample_indices, max_allele=3
            )
            count[:, cohort_index] = cohort_ac

            if nobs_mode == "called":
                nobs[:, cohort_index] = cohort_an
            elif nobs_mode == "fixed":
                nobs[:, cohort_index] = cohort.size * 2
            else:
                raise ValueError(f"Bad nobs_mode: {nobs_mode!r}")

        debug("compute frequency")
        with np.errstate(divide="ignore", invalid="ignore"):
            # ignore division warnings
            frequency = count / nobs

        debug("compute maximum frequency over cohorts")
        with warnings.catch_warnings():
            # ignore "All-NaN slice encountered" warnings
            warnings.simplefilter("ignore", category=RuntimeWarning)
            max_af = np.nanmax(frequency, axis=1)

        debug("make dataframe of SNPs")
        df_variants_cols = {
            "contig": variant_contig,
            "position": variant_position,
            "ref_allele": variant_ref_allele.astype("U1"),
            "alt_allele": variant_alt_allele.astype("U1"),
            "max_af": max_af,
        }
        for site_mask in self.site_mask_ids:
            df_variants_cols[f"pass_{site_mask}"] = variant_pass[site_mask]
        df_variants = pd.DataFrame(df_variants_cols)

        debug("deal with SNP alleles not observed")
        if drop_invariant:
            loc_variant = max_af > 0
            df_variants = df_variants.loc[loc_variant].reset_index(drop=True)
            count = np.compress(loc_variant, count, axis=0)
            nobs = np.compress(loc_variant, nobs, axis=0)
            frequency = np.compress(loc_variant, frequency, axis=0)

        debug("set up variant effect annotator")
        ann = self._annotator()

        debug("add effects to the dataframe")
        ann.get_effects(
            transcript=transcript, variants=df_variants, progress=self._progress
        )

        debug("add variant labels")
        df_variants["label"] = self._pandas_apply(
            self._make_snp_label_effect,
            df_variants,
            columns=["contig", "position", "ref_allele", "alt_allele", "aa_change"],
        )

        debug("build the output dataset")
        ds_out = xr.Dataset()

        debug("cohort variables")
        for coh_col in df_cohorts.columns:
            ds_out[f"cohort_{coh_col}"] = "cohorts", df_cohorts[coh_col]

        debug("variant variables")
        for snp_col in df_variants.columns:
            ds_out[f"variant_{snp_col}"] = "variants", df_variants[snp_col]

        debug("event variables")
        ds_out["event_count"] = ("variants", "cohorts"), count
        ds_out["event_nobs"] = ("variants", "cohorts"), nobs
        ds_out["event_frequency"] = ("variants", "cohorts"), frequency

        debug("apply variant query")
        if variant_query is not None:
            loc_variants = df_variants.eval(variant_query).values
            ds_out = ds_out.isel(variants=loc_variants)

        debug("add confidence intervals")
        self._add_frequency_ci(ds_out, ci_method)

        debug("tidy up display by sorting variables")
        sorted_vars: List[str] = sorted([str(k) for k in ds_out.keys()])
        ds_out = ds_out[sorted_vars]

        debug("add metadata")
        gene_name = self._transcript_to_gene_name(transcript)
        title = transcript
        if gene_name:
            title += f" ({gene_name})"
        title += " SNP frequencies"
        ds_out.attrs["title"] = title

        return ds_out

    # Start of @staticmethod @abstractmethod

    @staticmethod
    @abstractmethod
    def _setup_taxon_colors(plot_kwargs=None):
        # Subclasses have different taxon_color_map.
        raise NotImplementedError("Must override _setup_taxon_colors")

    # Start of @staticmethod

    @staticmethod
    def _locate_cohorts(*, cohorts, df_samples):
        # build cohort dictionary where key=cohort_id, value=loc_coh
        coh_dict = {}

        if isinstance(cohorts, dict):
            # user has supplied a custom dictionary mapping cohort identifiers
            # to pandas queries

            for coh, query in cohorts.items():
                # locate samples
                loc_coh = df_samples.eval(query).values
                coh_dict[coh] = loc_coh

        if isinstance(cohorts, str):
            # user has supplied one of the predefined cohort sets

            # fix the string to match columns
            if not cohorts.startswith("cohort_"):
                cohorts = "cohort_" + cohorts

            # check the given cohort set exists
            if cohorts not in df_samples.columns:
                raise ValueError(f"{cohorts!r} is not a known cohort set")
            cohort_labels = df_samples[cohorts].unique()

            # remove the nans and sort
            cohort_labels = sorted([c for c in cohort_labels if isinstance(c, str)])
            for coh in cohort_labels:
                loc_coh = df_samples[cohorts] == coh
                coh_dict[coh] = loc_coh.values

        return coh_dict

    @staticmethod
    def _make_sample_period_month(row):
        year = row.year
        month = row.month
        if year > 0 and month > 0:
            return pd.Period(freq="M", year=year, month=month)
        else:
            return pd.NaT

    @staticmethod
    def _make_sample_period_quarter(row):
        year = row.year
        month = row.month
        if year > 0 and month > 0:
            return pd.Period(freq="Q", year=year, month=month)
        else:
            return pd.NaT

    @staticmethod
    def _make_sample_period_year(row):
        year = row.year
        if year > 0:
            return pd.Period(freq="A", year=year)
        else:
            return pd.NaT

    @staticmethod
    @numba.njit
    def _cohort_alt_allele_counts_melt_kernel(gt, indices, max_allele):
        n_variants = gt.shape[0]
        n_indices = indices.shape[0]
        ploidy = gt.shape[2]

        ac_alt_melt = np.zeros(n_variants * max_allele, dtype=np.int64)
        an = np.zeros(n_variants, dtype=np.int64)

        for i in range(n_variants):
            out_i_offset = (i * max_allele) - 1
            for j in range(n_indices):
                ix = indices[j]
                for k in range(ploidy):
                    allele = gt[i, ix, k]
                    if allele > 0:
                        out_i = out_i_offset + allele
                        ac_alt_melt[out_i] += 1
                        an[i] += 1
                    elif allele == 0:
                        an[i] += 1

        return ac_alt_melt, an

    @staticmethod
    def _make_snp_label(contig, position, ref_allele, alt_allele):
        return f"{contig}:{position:,} {ref_allele}>{alt_allele}"

    @staticmethod
    def _make_snp_label_effect(contig, position, ref_allele, alt_allele, aa_change):
        label = f"{contig}:{position:,} {ref_allele}>{alt_allele}"
        if isinstance(aa_change, str):
            label += f" ({aa_change})"
        return label

    @staticmethod
    def _make_snp_label_aa(aa_change, contig, position, ref_allele, alt_allele):
        label = f"{aa_change} ({contig}:{position:,} {ref_allele}>{alt_allele})"
        return label

    @staticmethod
    def _make_gene_cnv_label(gene_id, gene_name, cnv_type):
        label = gene_id
        if isinstance(gene_name, str):
            label += f" ({gene_name})"
        label += f" {cnv_type}"
        return label

    @staticmethod
    def _map_snp_to_aa_change_frq_ds(ds):
        # keep only variables that make sense for amino acid substitutions
        keep_vars = [
            "variant_contig",
            "variant_position",
            "variant_transcript",
            "variant_effect",
            "variant_impact",
            "variant_aa_pos",
            "variant_aa_change",
            "variant_ref_allele",
            "variant_ref_aa",
            "variant_alt_aa",
            "event_nobs",
        ]

        if ds.dims["variants"] == 1:
            # keep everything as-is, no need for aggregation
            ds_out = ds[keep_vars + ["variant_alt_allele", "event_count"]]

        else:
            # take the first value from all variants variables
            ds_out = ds[keep_vars].isel(variants=[0])

            # sum event count over variants
            count = ds["event_count"].values.sum(axis=0, keepdims=True)
            ds_out["event_count"] = ("variants", "cohorts"), count

            # collapse alt allele
            alt_allele = "{" + ",".join(ds["variant_alt_allele"].values) + "}"
            ds_out["variant_alt_allele"] = "variants", np.array(
                [alt_allele], dtype=object
            )

        return ds_out

    @staticmethod
    def _add_frequency_ci(ds, ci_method):
        from statsmodels.stats.proportion import proportion_confint

        if ci_method is not None:
            count = ds["event_count"].values
            nobs = ds["event_nobs"].values
            with np.errstate(divide="ignore", invalid="ignore"):
                frq_ci_low, frq_ci_upp = proportion_confint(
                    count=count, nobs=nobs, method=ci_method
                )
            ds["event_frequency_ci_low"] = ("variants", "cohorts"), frq_ci_low
            ds["event_frequency_ci_upp"] = ("variants", "cohorts"), frq_ci_upp

    @staticmethod
    def _build_cohorts_from_sample_grouping(group_samples_by_cohort, min_cohort_size):
        # build cohorts dataframe
        df_cohorts = group_samples_by_cohort.agg(
            size=("sample_id", len),
            lat_mean=("latitude", "mean"),
            lat_max=("latitude", "mean"),
            lat_min=("latitude", "mean"),
            lon_mean=("longitude", "mean"),
            lon_max=("longitude", "mean"),
            lon_min=("longitude", "mean"),
        )
        # reset index so that the index fields are included as columns
        df_cohorts = df_cohorts.reset_index()

        # add cohort helper variables
        cohort_period_start = df_cohorts["period"].apply(lambda v: v.start_time)
        cohort_period_end = df_cohorts["period"].apply(lambda v: v.end_time)
        df_cohorts["period_start"] = cohort_period_start
        df_cohorts["period_end"] = cohort_period_end
        # create a label that is similar to the cohort metadata,
        # although this won't be perfect
        df_cohorts["label"] = df_cohorts.apply(
            lambda v: f"{v.area}_{v.taxon[:4]}_{v.period}", axis="columns"
        )

        # apply minimum cohort size
        df_cohorts = df_cohorts.query(f"size >= {min_cohort_size}").reset_index(
            drop=True
        )

        return df_cohorts

    @staticmethod
    def _check_param_min_cohort_size(min_cohort_size):
        if not isinstance(min_cohort_size, int):
            raise TypeError(
                f"Type of parameter min_cohort_size must be int; found {type(min_cohort_size)}."
            )
        if min_cohort_size < 1:
            raise ValueError(
                f"Value of parameter min_cohort_size must be at least 1; found {min_cohort_size}."
            )

    @staticmethod
    def _pandas_apply(f, df, columns):
        """Optimised alternative to pandas apply."""
        df = df.reset_index(drop=True)
        iterator = zip(*[df[c].values for c in columns])
        ret = pd.Series((f(*vals) for vals in iterator))
        return ret

    @staticmethod
    def _roh_hmm_predict(
        *,
        windows,
        counts,
        phet_roh,
        phet_nonroh,
        transition,
        window_size,
        sample_id,
        contig,
    ):
        # This implementation is based on scikit-allel, but modified to use
        # moving window computation of het counts.
        from allel.stats.misc import tabulate_state_blocks
        from allel.stats.roh import _hmm_derive_transition_matrix

        # Protopunica is pomegranate frozen at version 0.14.8, wich is compatible
        # with the code here. Also protopunica has binary wheels available from
        # PyPI and so installs much faster.
        from protopunica import HiddenMarkovModel, PoissonDistribution

        # het probabilities
        het_px = np.concatenate([(phet_roh,), phet_nonroh])

        # start probabilities (all equal)
        start_prob = np.repeat(1 / het_px.size, het_px.size)

        # transition between underlying states
        transition_mx = _hmm_derive_transition_matrix(transition, het_px.size)

        # emission probability distribution
        dists = [PoissonDistribution(x * window_size) for x in het_px]

        # set up model
        # noinspection PyArgumentList
        model = HiddenMarkovModel.from_matrix(
            transition_probabilities=transition_mx,
            distributions=dists,
            starts=start_prob,
        )

        # predict hidden states
        prediction = np.array(model.predict(counts[:, None]))

        # tabulate runs of homozygosity (state 0)
        # noinspection PyTypeChecker
        df_blocks = tabulate_state_blocks(prediction, states=list(range(len(het_px))))
        df_roh = df_blocks[(df_blocks["state"] == 0)].reset_index(drop=True)

        # adapt the dataframe for ROH
        df_roh["sample_id"] = sample_id
        df_roh["contig"] = contig
        df_roh["roh_start"] = df_roh["start_ridx"].apply(lambda y: windows[y, 0])
        df_roh["roh_stop"] = df_roh["stop_lidx"].apply(lambda y: windows[y, 1])
        df_roh["roh_length"] = df_roh["roh_stop"] - df_roh["roh_start"]
        df_roh.rename(columns={"is_marginal": "roh_is_marginal"}, inplace=True)

        return df_roh[
            [
                "sample_id",
                "contig",
                "roh_start",
                "roh_stop",
                "roh_length",
                "roh_is_marginal",
            ]
        ]

    def _snp_df(self, *, transcript: str) -> Tuple[Region, pd.DataFrame]:
        """Set up a dataframe with SNP site and filter columns."""
        debug = self._log.debug

        debug("get feature direct from genome_features")
        gs = self.genome_features()
        feature = gs[gs["ID"] == transcript].squeeze()
        if feature.empty:
            raise ValueError(
                f"No genome feature ID found matching transcript {transcript}"
            )
        contig = feature.contig
        region = Region(contig, feature.start, feature.end)

        debug("grab pos, ref and alt for chrom arm from snp_sites")
        pos = self.snp_sites(region=contig, field="POS")
        ref = self.snp_sites(region=contig, field="REF")
        alt = self.snp_sites(region=contig, field="ALT")
        loc_feature = locate_region(region, pos)
        pos = pos[loc_feature].compute()
        ref = ref[loc_feature].compute()
        alt = alt[loc_feature].compute()

        debug("access site filters")
        filter_pass = dict()
        masks = self.site_mask_ids
        for m in masks:
            x = self.site_filters(region=contig, mask=m)
            x = x[loc_feature].compute()
            filter_pass[m] = x

        debug("set up columns with contig, pos, ref, alt columns")
        cols = {
            "contig": contig,
            "position": np.repeat(pos, 3),
            "ref_allele": np.repeat(ref.astype("U1"), 3),
            "alt_allele": alt.astype("U1").flatten(),
        }

        debug("add mask columns")
        for m in masks:
            x = filter_pass[m]
            cols[f"pass_{m}"] = np.repeat(x, 3)

        debug("construct dataframe")
        df_snps = pd.DataFrame(cols)

        return region, df_snps

    def _annotator(self):
        """Set up variant effect annotator."""
        if self._cache_annotator is None:
            self._cache_annotator = veff.Annotator(
                genome=self.open_genome(), genome_features=self.genome_features()
            )
        return self._cache_annotator

    @check_types
    @doc(
        summary="Compute variant effects for a gene transcript.",
        returns="""
            A dataframe of all possible SNP variants and their effects, one row
            per variant.
        """,
    )
    def snp_effects(
        self,
        transcript: base_params.transcript,
        site_mask: Optional[base_params.site_mask] = None,
    ) -> pd.DataFrame:
        debug = self._log.debug

        debug("setup initial dataframe of SNPs")
        _, df_snps = self._snp_df(transcript=transcript)

        debug("setup variant effect annotator")
        ann = self._annotator()

        debug("apply mask if requested")
        if site_mask is not None:
            loc_sites = df_snps[f"pass_{site_mask}"]
            df_snps = df_snps.loc[loc_sites]

        debug("reset index after filtering")
        df_snps.reset_index(inplace=True, drop=True)

        debug("add effects to the dataframe")
        ann.get_effects(transcript=transcript, variants=df_snps)

        return df_snps

    @check_types
    @doc(
        summary="Create an IGV browser and inject into the current notebook.",
        parameters=dict(
            tracks="Configuration for any additional tracks.",
        ),
        returns="IGV browser.",
    )
    def igv(
        self, region: base_params.single_region, tracks: Optional[List] = None
    ) -> igv_notebook.Browser:
        debug = self._log.debug

        debug("resolve region")
        region_prepped: Region = parse_single_region(self, region)
        del region

        config = {
            "reference": {
                "id": self._genome_ref_id,
                "name": self._genome_ref_name,
                "fastaURL": f"{self._gcs_url}{self._genome_fasta_path}",
                "indexURL": f"{self._gcs_url}{self._genome_fai_path}",
                "tracks": [
                    {
                        "name": "Genes",
                        "type": "annotation",
                        "format": "gff3",
                        "url": f"{self._gcs_url}{self._geneset_gff3_path}",
                        "indexed": False,
                    }
                ],
            },
            "locus": str(region_prepped),
        }
        if tracks:
            config["tracks"] = tracks

        debug(config)

        igv_notebook.init()
        browser = igv_notebook.Browser(config)

        return browser

    @check_types
    @doc(
        summary="""
            Launch IGV and view sequence read alignments and SNP genotypes from
            the given sample.
        """,
        parameters=dict(
            sample="Sample identifier.",
            visibility_window="""
                Zoom level in base pairs at which alignment and SNP data will become
                visible.
            """,
        ),
    )
    def view_alignments(
        self,
        region: base_params.single_region,
        sample: str,
        visibility_window: int = 20_000,
    ):
        debug = self._log.debug

        debug("look up sample set for sample")
        sample_rec = self.sample_metadata().set_index("sample_id").loc[sample]
        sample_set = sample_rec["sample_set"]

        debug("load data catalog")
        df_cat = self.wgs_data_catalog(sample_set=sample_set)

        debug("locate record for sample")
        cat_rec = df_cat.set_index("sample_id").loc[sample]
        bam_url = cat_rec["alignments_bam"]
        vcf_url = cat_rec["snp_genotypes_vcf"]
        debug(bam_url)
        debug(vcf_url)

        debug("parse region")
        resolved_region: Region = parse_single_region(self, region)
        del region
        contig = resolved_region.contig

        # begin creating tracks
        tracks: List[Dict] = []

        # https://github.com/igvteam/igv-notebook/issues/3 -- resolved now
        debug("set up site filters tracks")
        self._view_alignments_add_site_filters_tracks(
            contig=contig, visibility_window=visibility_window, tracks=tracks
        )

        debug("add SNPs track")
        tracks.append(
            {
                "name": "SNPs",
                "url": vcf_url,
                "indexURL": f"{vcf_url}.tbi",
                "format": "vcf",
                "type": "variant",
                "visibilityWindow": visibility_window,  # bp
                "height": 50,
            }
        )

        debug("add alignments track")
        tracks.append(
            {
                "name": "Alignments",
                "url": bam_url,
                "indexURL": f"{bam_url}.bai",
                "format": "bam",
                "type": "alignment",
                "visibilityWindow": visibility_window,  # bp
                "height": 500,
            }
        )

        debug("create IGV browser")
        self.igv(region=resolved_region, tracks=tracks)

    def _pca(
        self,
        *,
        region,
        n_snps,
        thin_offset,
        sample_sets,
        sample_indices,
        site_mask,
        min_minor_ac,
        max_missing_an,
        n_components,
    ):
        debug = self._log.debug

        debug("access SNP calls")
        ds_snps = self.snp_calls(
            region=region,
            sample_sets=sample_sets,
            sample_indices=sample_indices,
            site_mask=site_mask,
        )
        debug(
            f"{ds_snps.dims['variants']:,} variants, {ds_snps.dims['samples']:,} samples"
        )

        debug("perform allele count")
        ac = self.snp_allele_counts(
            region=region,
            sample_sets=sample_sets,
            sample_indices=sample_indices,
            site_mask=site_mask,
        )
        n_chroms = ds_snps.dims["samples"] * 2
        an_called = ac.sum(axis=1)
        an_missing = n_chroms - an_called

        debug("ascertain sites")
        ac = allel.AlleleCountsArray(ac)
        min_ref_ac = min_minor_ac
        max_ref_ac = n_chroms - min_minor_ac
        # here we choose biallelic sites involving the reference allele
        loc_sites = (
            ac.is_biallelic()
            & (ac[:, 0] >= min_ref_ac)
            & (ac[:, 0] <= max_ref_ac)
            & (an_missing <= max_missing_an)
        )
        debug(f"ascertained {np.count_nonzero(loc_sites):,} sites")

        debug("thin sites to approximately desired number")
        loc_sites = np.nonzero(loc_sites)[0]
        thin_step = max(loc_sites.shape[0] // n_snps, 1)
        loc_sites_thinned = loc_sites[thin_offset::thin_step]
        debug(f"thinned to {np.count_nonzero(loc_sites_thinned):,} sites")

        debug("access genotypes")
        gt = ds_snps["call_genotype"].data
        gt_asc = da.take(gt, loc_sites_thinned, axis=0)
        gn_asc = allel.GenotypeDaskArray(gt_asc).to_n_alt()
        with self._dask_progress(desc="Load SNP genotypes"):
            gn_asc = gn_asc.compute()

        debug("remove any sites where all genotypes are identical")
        loc_var = np.any(gn_asc != gn_asc[:, 0, np.newaxis], axis=1)
        gn_var = np.compress(loc_var, gn_asc, axis=0)
        debug(f"final shape {gn_var.shape}")

        debug("run the PCA")
        coords, model = allel.pca(gn_var, n_components=n_components)

        debug("work around sign indeterminacy")
        for i in range(n_components):
            c = coords[:, i]
            if np.abs(c.min()) > np.abs(c.max()):
                coords[:, i] = c * -1

        results = dict(coords=coords, evr=model.explained_variance_ratio_)
        return results

    @check_types
    @doc(
        summary="""
            Plot explained variance ratios from a principal components analysis
            (PCA) using a plotly bar plot.
        """,
        parameters=dict(
            kwargs="Passed through to px.bar().",
        ),
    )
    def plot_pca_variance(
        self,
        evr: pca_params.evr,
        width: plotly_params.width = 900,
        height: plotly_params.height = 400,
        show: plotly_params.show = True,
        renderer: plotly_params.renderer = None,
        **kwargs,
    ) -> plotly_params.figure:
        debug = self._log.debug

        debug("prepare plotting variables")
        y = evr * 100  # convert to percent
        x = [str(i + 1) for i in range(len(y))]

        debug("set up plotting options")
        plot_kwargs = dict(
            labels={
                "x": "Principal component",
                "y": "Explained variance (%)",
            },
            template="simple_white",
            width=width,
            height=height,
        )
        debug("apply any user overrides")
        plot_kwargs.update(kwargs)

        debug("make a bar plot")
        fig = px.bar(x=x, y=y, **plot_kwargs)

        if show:
            fig.show(renderer=renderer)
            return None
        else:
            return fig

    def _cohort_alt_allele_counts_melt(self, gt, indices, max_allele):
        ac_alt_melt, an = self._cohort_alt_allele_counts_melt_kernel(
            gt, indices, max_allele
        )
        an_melt = np.repeat(an, max_allele, axis=0)
        return ac_alt_melt, an_melt

    def _prep_samples_for_cohort_grouping(self, *, df_samples, area_by, period_by):
        # take a copy, as we will modify the dataframe
        df_samples = df_samples.copy()

        # fix intermediate taxon values - we only want to build cohorts with clean
        # taxon calls, so we set intermediate values to None
        loc_intermediate_taxon = (
            df_samples["taxon"].str.startswith("intermediate").fillna(False)
        )
        df_samples.loc[loc_intermediate_taxon, "taxon"] = None

        # add period column
        if period_by == "year":
            make_period = self._make_sample_period_year
        elif period_by == "quarter":
            make_period = self._make_sample_period_quarter
        elif period_by == "month":
            make_period = self._make_sample_period_month
        else:
            raise ValueError(
                f"Value for period_by parameter must be one of 'year', 'quarter', 'month'; found {period_by!r}."
            )
        sample_period = df_samples.apply(make_period, axis="columns")
        df_samples["period"] = sample_period

        # add area column for consistent output
        df_samples["area"] = df_samples[area_by]

        return df_samples

    def _lookup_sample(
        self,
        sample: het_params.single_sample,
        sample_set: Optional[base_params.sample_set] = None,
    ):
        df_samples = self.sample_metadata(sample_sets=sample_set).set_index("sample_id")
        sample_rec = None
        if isinstance(sample, str):
            sample_rec = df_samples.loc[sample]
        else:
            assert isinstance(sample, int)
            sample_rec = df_samples.iloc[sample]
        return sample_rec

    def _plot_heterozygosity_track(
        self,
        *,
        sample_id,
        sample_set,
        windows,
        counts,
        region: Region,
        window_size,
        y_max,
        sizing_mode,
        width,
        height,
        circle_kwargs,
        show,
        x_range,
        output_backend,
    ):
        debug = self._log.debug

        # pos axis
        window_pos = windows.mean(axis=1)

        # het axis
        window_het = counts / window_size

        # determine plotting limits
        if x_range is None:
            if region.start is not None:
                x_min = region.start
            else:
                x_min = 0
            if region.end is not None:
                x_max = region.end
            else:
                x_max = len(self.genome_sequence(region.contig))
            x_range = bokeh.models.Range1d(x_min, x_max, bounds="auto")

        debug("create a figure for plotting")
        xwheel_zoom = bokeh.models.WheelZoomTool(
            dimensions="width", maintain_focus=False
        )
        fig = bokeh.plotting.figure(
            title=f"{sample_id} ({sample_set})",
            tools=["xpan", "xzoom_in", "xzoom_out", xwheel_zoom, "reset"],
            active_scroll=xwheel_zoom,
            active_drag="xpan",
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            toolbar_location="above",
            x_range=x_range,
            y_range=(0, y_max),
            output_backend=output_backend,
        )

        debug("plot heterozygosity")
        data = pd.DataFrame(
            {
                "position": window_pos,
                "heterozygosity": window_het,
            }
        )
        if circle_kwargs is None:
            circle_kwargs = dict()
        circle_kwargs.setdefault("size", 4)
        circle_kwargs.setdefault("line_width", 0)
        fig.circle(x="position", y="heterozygosity", source=data, **circle_kwargs)

        debug("tidy up the plot")
        fig.yaxis.axis_label = "Heterozygosity (bp⁻¹)"
        self._bokeh_style_genome_xaxis(fig, region.contig)

        if show:
            bokeh.plotting.show(fig)

        return fig

    @check_types
    @doc(
        summary="Plot windowed heterozygosity for a single sample over a genome region.",
    )
    def plot_heterozygosity_track(
        self,
        sample: het_params.single_sample,
        region: base_params.single_region,
        window_size: het_params.window_size = het_params.window_size_default,
        y_max: het_params.y_max = het_params.y_max_default,
        circle_kwargs: Optional[het_params.circle_kwargs] = None,
        site_mask: base_params.site_mask = DEFAULT,
        sample_set: Optional[base_params.sample_set] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        height: gplt_params.height = 200,
        show: gplt_params.show = True,
        x_range: Optional[gplt_params.x_range] = None,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        debug = self._log.debug

        # Normalise parameters.
        region_prepped: Region = parse_single_region(self, region)
        del region

        debug("compute windowed heterozygosity")
        sample_id, sample_set, windows, counts = self._sample_count_het(
            sample=sample,
            region=region_prepped,
            site_mask=site_mask,
            window_size=window_size,
            sample_set=sample_set,
        )

        debug("plot heterozygosity")
        fig = self._plot_heterozygosity_track(
            sample_id=sample_id,
            sample_set=sample_set,
            windows=windows,
            counts=counts,
            region=region_prepped,
            window_size=window_size,
            y_max=y_max,
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            circle_kwargs=circle_kwargs,
            show=show,
            x_range=x_range,
            output_backend=output_backend,
        )

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Plot windowed heterozygosity for a single sample over a genome region.",
    )
    def plot_heterozygosity(
        self,
        sample: het_params.sample,
        region: base_params.single_region,
        window_size: het_params.window_size = het_params.window_size_default,
        y_max: het_params.y_max = het_params.y_max_default,
        circle_kwargs: Optional[het_params.circle_kwargs] = None,
        site_mask: base_params.site_mask = DEFAULT,
        sample_set: Optional[base_params.sample_set] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        track_height: gplt_params.track_height = 170,
        genes_height: gplt_params.genes_height = gplt_params.genes_height_default,
        show: gplt_params.show = True,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        debug = self._log.debug

        # normalise to support multiple samples
        if isinstance(sample, (list, tuple)):
            samples = sample
        else:
            samples = [sample]

        debug("plot first sample track")
        fig1 = self.plot_heterozygosity_track(
            sample=samples[0],
            sample_set=sample_set,
            region=region,
            site_mask=site_mask,
            window_size=window_size,
            y_max=y_max,
            sizing_mode=sizing_mode,
            width=width,
            height=track_height,
            circle_kwargs=circle_kwargs,
            show=False,
            output_backend=output_backend,
        )
        fig1.xaxis.visible = False
        figs = [fig1]

        debug("plot remaining sample tracks")
        for sample in samples[1:]:
            fig_het = self.plot_heterozygosity_track(
                sample=sample,
                sample_set=sample_set,
                region=region,
                site_mask=site_mask,
                window_size=window_size,
                y_max=y_max,
                sizing_mode=sizing_mode,
                width=width,
                height=track_height,
                circle_kwargs=circle_kwargs,
                show=False,
                x_range=fig1.x_range,
                output_backend=output_backend,
            )
            fig_het.xaxis.visible = False
            figs.append(fig_het)

        debug("plot genes track")
        fig_genes = self.plot_genes(
            region=region,
            sizing_mode=sizing_mode,
            width=width,
            height=genes_height,
            x_range=fig1.x_range,
            show=False,
            output_backend=output_backend,
        )
        figs.append(fig_genes)

        debug("combine plots into a single figure")
        fig_all = bokeh.layouts.gridplot(
            figs,
            ncols=1,
            toolbar_location="above",
            merge_tools=True,
            sizing_mode=sizing_mode,
        )

        if show:
            bokeh.plotting.show(fig_all)
            return None
        else:
            return fig_all

    def _sample_count_het(
        self,
        sample: het_params.single_sample,
        region: Region,
        site_mask: base_params.site_mask,
        window_size: het_params.window_size,
        sample_set: Optional[base_params.sample_set] = None,
    ):
        debug = self._log.debug

        site_mask = self._prep_site_mask_param(site_mask=site_mask)

        debug("access sample metadata, look up sample")
        sample_rec = self._lookup_sample(sample=sample, sample_set=sample_set)
        sample_id = sample_rec.name  # sample_id
        sample_set = sample_rec["sample_set"]

        debug("access SNPs, select data for sample")
        ds_snps = self.snp_calls(
            region=region, sample_sets=sample_set, site_mask=site_mask
        )
        ds_snps_sample = ds_snps.set_index(samples="sample_id").sel(samples=sample_id)

        # snp positions
        pos = ds_snps_sample["variant_position"].values

        # access genotypes
        gt = allel.GenotypeDaskVector(ds_snps_sample["call_genotype"].data)

        # compute het
        with self._dask_progress(desc="Compute heterozygous genotypes"):
            is_het = gt.is_het().compute()

        # compute window coordinates
        windows = allel.moving_statistic(
            values=pos,
            statistic=lambda x: [x[0], x[-1]],
            size=window_size,
        )

        # compute windowed heterozygosity
        counts = allel.moving_statistic(
            values=is_het,
            statistic=np.sum,
            size=window_size,
        )

        return sample_id, sample_set, windows, counts

    @check_types
    @doc(
        summary="Infer runs of homozygosity for a single sample over a genome region.",
    )
    def roh_hmm(
        self,
        sample: het_params.single_sample,
        region: base_params.single_region,
        window_size: het_params.window_size = het_params.window_size_default,
        site_mask: base_params.site_mask = DEFAULT,
        sample_set: Optional[base_params.sample_set] = None,
        phet_roh: het_params.phet_roh = het_params.phet_roh_default,
        phet_nonroh: het_params.phet_nonroh = het_params.phet_nonroh_default,
        transition: het_params.transition = het_params.transition_default,
    ) -> het_params.df_roh:
        debug = self._log.debug

        resolved_region: Region = parse_single_region(self, region)
        del region

        debug("compute windowed heterozygosity")
        sample_id, sample_set, windows, counts = self._sample_count_het(
            sample=sample,
            region=resolved_region,
            site_mask=site_mask,
            window_size=window_size,
            sample_set=sample_set,
        )

        debug("compute runs of homozygosity")
        df_roh = self._roh_hmm_predict(
            windows=windows,
            counts=counts,
            phet_roh=phet_roh,
            phet_nonroh=phet_nonroh,
            transition=transition,
            window_size=window_size,
            sample_id=sample_id,
            contig=resolved_region.contig,
        )

        return df_roh

    @check_types
    @doc(
        summary="Plot a runs of homozygosity track.",
    )
    def plot_roh_track(
        self,
        df_roh: het_params.df_roh,
        region: base_params.single_region,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        height: gplt_params.height = 80,
        show: gplt_params.show = True,
        x_range: Optional[gplt_params.x_range] = None,
        title: Optional[gplt_params.title] = None,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        debug = self._log.debug

        debug("handle region parameter - this determines the genome region to plot")
        resolved_region: Region = parse_single_region(self, region)
        del region
        contig = resolved_region.contig
        start = resolved_region.start
        end = resolved_region.end
        if start is None:
            start = 0
        if end is None:
            end = len(self.genome_sequence(contig))

        debug("define x axis range")
        if x_range is None:
            x_range = bokeh.models.Range1d(start, end, bounds="auto")

        debug(
            "we're going to plot each gene as a rectangle, so add some additional columns"
        )
        data = df_roh.copy()
        data["bottom"] = 0.2
        data["top"] = 0.8

        debug("make a figure")
        xwheel_zoom = bokeh.models.WheelZoomTool(
            dimensions="width", maintain_focus=False
        )
        fig = bokeh.plotting.figure(
            title=title,
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            tools=[
                "xpan",
                "xzoom_in",
                "xzoom_out",
                xwheel_zoom,
                "reset",
                "tap",
                "hover",
            ],
            active_scroll=xwheel_zoom,
            active_drag="xpan",
            x_range=x_range,
            y_range=bokeh.models.Range1d(0, 1),
            output_backend=output_backend,
        )

        debug("now plot the ROH as rectangles")
        fig.quad(
            bottom="bottom",
            top="top",
            left="roh_start",
            right="roh_stop",
            source=data,
            line_width=1,
            fill_alpha=0.5,
        )

        debug("tidy up the plot")
        fig.ygrid.visible = False
        fig.yaxis.ticker = []
        fig.yaxis.axis_label = "RoH"
        self._bokeh_style_genome_xaxis(fig, resolved_region.contig)

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Plot windowed heterozygosity and inferred runs of homozygosity for a
            single sample over a genome region.
        """,
    )
    def plot_roh(
        self,
        sample: het_params.single_sample,
        region: base_params.single_region,
        window_size: het_params.window_size = het_params.window_size_default,
        site_mask: base_params.site_mask = DEFAULT,
        sample_set: Optional[base_params.sample_set] = None,
        phet_roh: het_params.phet_roh = het_params.phet_roh_default,
        phet_nonroh: het_params.phet_nonroh = het_params.phet_nonroh_default,
        transition: het_params.transition = het_params.transition_default,
        y_max: het_params.y_max = het_params.y_max_default,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        heterozygosity_height: gplt_params.height = 170,
        roh_height: gplt_params.height = 40,
        genes_height: gplt_params.genes_height = gplt_params.genes_height_default,
        circle_kwargs: Optional[het_params.circle_kwargs] = None,
        show: gplt_params.show = True,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        debug = self._log.debug

        resolved_region: Region = parse_single_region(self, region)
        del region

        debug("compute windowed heterozygosity")
        sample_id, sample_set, windows, counts = self._sample_count_het(
            sample=sample,
            region=resolved_region,
            site_mask=site_mask,
            window_size=window_size,
            sample_set=sample_set,
        )

        debug("plot_heterozygosity track")
        fig_het = self._plot_heterozygosity_track(
            sample_id=sample_id,
            sample_set=sample_set,
            windows=windows,
            counts=counts,
            region=resolved_region,
            window_size=window_size,
            y_max=y_max,
            sizing_mode=sizing_mode,
            width=width,
            height=heterozygosity_height,
            circle_kwargs=circle_kwargs,
            show=False,
            x_range=None,
            output_backend=output_backend,
        )
        fig_het.xaxis.visible = False
        figs = [fig_het]

        debug("compute runs of homozygosity")
        df_roh = self._roh_hmm_predict(
            windows=windows,
            counts=counts,
            phet_roh=phet_roh,
            phet_nonroh=phet_nonroh,
            transition=transition,
            window_size=window_size,
            sample_id=sample_id,
            contig=resolved_region.contig,
        )

        debug("plot roh track")
        fig_roh = self.plot_roh_track(
            df_roh,
            region=resolved_region,
            sizing_mode=sizing_mode,
            width=width,
            height=roh_height,
            show=False,
            x_range=fig_het.x_range,
            output_backend=output_backend,
        )
        fig_roh.xaxis.visible = False
        figs.append(fig_roh)

        debug("plot genes track")
        fig_genes = self.plot_genes(
            region=resolved_region,
            sizing_mode=sizing_mode,
            width=width,
            height=genes_height,
            x_range=fig_het.x_range,
            show=False,
            output_backend=output_backend,
        )
        figs.append(fig_genes)

        debug("combine plots into a single figure")
        fig_all = bokeh.layouts.gridplot(
            figs,
            ncols=1,
            toolbar_location="above",
            merge_tools=True,
            sizing_mode=sizing_mode,
        )

        if show:
            bokeh.plotting.show(fig_all)
            return None
        else:
            return fig_all

    @check_types
    @doc(
        summary="""
            Run a principal components analysis (PCA) using biallelic SNPs from
            the selected genome region and samples.
        """,
        returns=("df_pca", "evr"),
        notes="""
            This computation may take some time to run, depending on your computing
            environment. Results of this computation will be cached and re-used if
            the `results_cache` parameter was set when instantiating the Ag3 class.
        """,
    )
    def pca(
        self,
        region: base_params.region,
        n_snps: pca_params.n_snps,
        thin_offset: pca_params.thin_offset = pca_params.thin_offset_default,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        site_mask: Optional[base_params.site_mask] = DEFAULT,
        min_minor_ac: pca_params.min_minor_ac = pca_params.min_minor_ac_default,
        max_missing_an: pca_params.max_missing_an = pca_params.max_missing_an_default,
        n_components: pca_params.n_components = pca_params.n_components_default,
    ) -> Tuple[pca_params.df_pca, pca_params.evr]:
        debug = self._log.debug

        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._pca_results_cache_name

        debug("normalize params for consistent hash value")
        (
            sample_sets_prepped,
            sample_indices_prepped,
        ) = self._prep_sample_selection_cache_params(
            sample_sets=sample_sets,
            sample_query=sample_query,
            sample_indices=None,
        )
        region_prepped = self._prep_region_cache_param(region=region)
        site_mask_prepped = self._prep_optional_site_mask_param(site_mask=site_mask)
        params = dict(
            region=region_prepped,
            n_snps=n_snps,
            thin_offset=thin_offset,
            sample_sets=sample_sets_prepped,
            sample_indices=sample_indices_prepped,
            site_mask=site_mask_prepped,
            min_minor_ac=min_minor_ac,
            max_missing_an=max_missing_an,
            n_components=n_components,
        )

        debug("try to retrieve results from the cache")
        try:
            results = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            results = self._pca(**params)
            self.results_cache_set(name=name, params=params, results=results)

        debug("unpack results")
        coords = results["coords"]
        evr = results["evr"]

        debug("add coords to sample metadata dataframe")
        df_samples = self.sample_metadata(
            sample_sets=sample_sets,
            sample_indices=sample_indices_prepped,
        )
        df_coords = pd.DataFrame(
            {f"PC{i + 1}": coords[:, i] for i in range(n_components)}
        )
        df_pca = pd.concat([df_samples, df_coords], axis="columns")

        return df_pca, evr

    @check_types
    @doc(
        summary="""
            Compute SNP allele frequencies for a gene transcript.
        """,
        returns="""
            A dataframe of SNP allele frequencies, one row per variant allele.
        """,
        notes="""
            Cohorts with fewer samples than `min_cohort_size` will be excluded from
            output data frame.
        """,
    )
    def snp_allele_frequencies(
        self,
        transcript: base_params.transcript,
        cohorts: base_params.cohorts,
        sample_query: Optional[base_params.sample_query] = None,
        min_cohort_size: base_params.min_cohort_size = 10,
        site_mask: Optional[base_params.site_mask] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        drop_invariant: frq_params.drop_invariant = True,
        effects: frq_params.effects = True,
    ) -> pd.DataFrame:
        debug = self._log.debug

        debug("check parameters")
        self._check_param_min_cohort_size(min_cohort_size)

        debug("access sample metadata")
        df_samples = self.sample_metadata(
            sample_sets=sample_sets, sample_query=sample_query
        )

        debug("setup initial dataframe of SNPs")
        region, df_snps = self._snp_df(transcript=transcript)

        debug("get genotypes")
        gt = self.snp_genotypes(
            region=region,
            sample_sets=sample_sets,
            sample_query=sample_query,
            field="GT",
        )

        debug("slice to feature location")
        with self._dask_progress(desc="Load SNP genotypes"):
            gt = gt.compute()

        debug("build coh dict")
        coh_dict = self._locate_cohorts(cohorts=cohorts, df_samples=df_samples)

        debug("count alleles")
        freq_cols = dict()
        cohorts_iterator = self._progress(
            coh_dict.items(), desc="Compute allele frequencies"
        )
        for coh, loc_coh in cohorts_iterator:
            n_samples = np.count_nonzero(loc_coh)
            debug(f"{coh}, {n_samples} samples")
            if n_samples >= min_cohort_size:
                gt_coh = np.compress(loc_coh, gt, axis=1)
                ac_coh = allel.GenotypeArray(gt_coh).count_alleles(max_allele=3)
                af_coh = ac_coh.to_frequencies()
                freq_cols["frq_" + coh] = af_coh[:, 1:].flatten()

        debug("build a dataframe with the frequency columns")
        df_freqs = pd.DataFrame(freq_cols)

        debug("compute max_af")
        df_max_af = pd.DataFrame({"max_af": df_freqs.max(axis=1)})

        debug("build the final dataframe")
        df_snps.reset_index(drop=True, inplace=True)
        df_snps = pd.concat([df_snps, df_freqs, df_max_af], axis=1)

        debug("apply site mask if requested")
        if site_mask is not None:
            loc_sites = df_snps[f"pass_{site_mask}"]
            df_snps = df_snps.loc[loc_sites]

        debug("drop invariants")
        if drop_invariant:
            loc_variant = df_snps["max_af"] > 0
            df_snps = df_snps.loc[loc_variant]

        debug("reset index after filtering")
        df_snps.reset_index(inplace=True, drop=True)

        if effects:
            debug("add effect annotations")
            ann = self._annotator()
            ann.get_effects(
                transcript=transcript, variants=df_snps, progress=self._progress
            )

            debug("add label")
            df_snps["label"] = self._pandas_apply(
                self._make_snp_label_effect,
                df_snps,
                columns=["contig", "position", "ref_allele", "alt_allele", "aa_change"],
            )

            debug("set index")
            df_snps.set_index(
                ["contig", "position", "ref_allele", "alt_allele", "aa_change"],
                inplace=True,
            )

        else:
            debug("add label")
            df_snps["label"] = self._pandas_apply(
                self._make_snp_label,
                df_snps,
                columns=["contig", "position", "ref_allele", "alt_allele"],
            )

            debug("set index")
            df_snps.set_index(
                ["contig", "position", "ref_allele", "alt_allele"],
                inplace=True,
            )

        debug("add dataframe metadata")
        gene_name = self._transcript_to_gene_name(transcript)
        title = transcript
        if gene_name:
            title += f" ({gene_name})"
        title += " SNP frequencies"
        df_snps.attrs["title"] = title

        return df_snps

    @check_types
    @doc(
        summary="""
            Compute amino acid substitution frequencies for a gene transcript.
        """,
        returns="""
            A dataframe of amino acid allele frequencies, one row per
            substitution.
        """,
        notes="""
            Cohorts with fewer samples than `min_cohort_size` will be excluded from
            output data frame.
        """,
    )
    def aa_allele_frequencies(
        self,
        transcript: base_params.transcript,
        cohorts: base_params.cohorts,
        sample_query: Optional[base_params.sample_query] = None,
        min_cohort_size: Optional[base_params.min_cohort_size] = 10,
        site_mask: Optional[base_params.site_mask] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        drop_invariant: frq_params.drop_invariant = True,
    ) -> pd.DataFrame:
        debug = self._log.debug

        df_snps = self.snp_allele_frequencies(
            transcript=transcript,
            cohorts=cohorts,
            sample_query=sample_query,
            min_cohort_size=min_cohort_size,
            site_mask=site_mask,
            sample_sets=sample_sets,
            drop_invariant=drop_invariant,
            effects=True,
        )
        df_snps.reset_index(inplace=True)

        # we just want aa change
        df_ns_snps = df_snps.query(AA_CHANGE_QUERY).copy()

        # N.B., we need to worry about the possibility of the
        # same aa change due to SNPs at different positions. We cannot
        # sum frequencies of SNPs at different genomic positions. This
        # is why we group by position and aa_change, not just aa_change.

        debug("group and sum to collapse multi variant allele changes")
        freq_cols = [col for col in df_ns_snps if col.startswith("frq")]
        agg: Dict[str, Union[Callable, str]] = {c: np.nansum for c in freq_cols}
        keep_cols = (
            "contig",
            "transcript",
            "aa_pos",
            "ref_allele",
            "ref_aa",
            "alt_aa",
            "effect",
            "impact",
        )
        for c in keep_cols:
            agg[c] = "first"
        agg["alt_allele"] = lambda v: "{" + ",".join(v) + "}" if len(v) > 1 else v
        df_aaf = df_ns_snps.groupby(["position", "aa_change"]).agg(agg).reset_index()

        debug("compute new max_af")
        df_aaf["max_af"] = df_aaf[freq_cols].max(axis=1)

        debug("add label")
        df_aaf["label"] = self._pandas_apply(
            self._make_snp_label_aa,
            df_aaf,
            columns=["aa_change", "contig", "position", "ref_allele", "alt_allele"],
        )

        debug("sort by genomic position")
        df_aaf = df_aaf.sort_values(["position", "aa_change"])

        debug("set index")
        df_aaf.set_index(["aa_change", "contig", "position"], inplace=True)

        debug("add metadata")
        gene_name = self._transcript_to_gene_name(transcript)
        title = transcript
        if gene_name:
            title += f" ({gene_name})"
        title += " SNP frequencies"
        df_aaf.attrs["title"] = title

        return df_aaf

    @check_types
    @doc(
        summary="""
            Group samples by taxon, area (space) and period (time), then compute
            amino acid change allele frequencies.
        """,
        returns="""
            The resulting dataset contains data has dimensions "cohorts" and
            "variants". Variables prefixed with "cohort" are 1-dimensional
            arrays with data about the cohorts, such as the area, period, taxon
            and cohort size. Variables prefixed with "variant" are
            1-dimensional arrays with data about the variants, such as the
            contig, position, reference and alternate alleles. Variables
            prefixed with "event" are 2-dimensional arrays with the allele
            counts and frequency calculations.
        """,
    )
    def aa_allele_frequencies_advanced(
        self,
        transcript: base_params.transcript,
        area_by: frq_params.area_by,
        period_by: frq_params.period_by,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        min_cohort_size: base_params.min_cohort_size = 10,
        variant_query: Optional[frq_params.variant_query] = None,
        site_mask: Optional[base_params.site_mask] = None,
        nobs_mode: frq_params.nobs_mode = "called",
        ci_method: Optional[frq_params.ci_method] = "wilson",
    ) -> xr.Dataset:
        debug = self._log.debug

        debug("begin by computing SNP allele frequencies")
        ds_snp_frq = self.snp_allele_frequencies_advanced(
            transcript=transcript,
            area_by=area_by,
            period_by=period_by,
            sample_sets=sample_sets,
            sample_query=sample_query,
            min_cohort_size=min_cohort_size,
            drop_invariant=True,  # always drop invariant for aa frequencies
            variant_query=AA_CHANGE_QUERY,  # we'll also apply a variant query later
            site_mask=site_mask,
            nobs_mode=nobs_mode,
            ci_method=None,  # we will recompute confidence intervals later
        )

        # N.B., we need to worry about the possibility of the
        # same aa change due to SNPs at different positions. We cannot
        # sum frequencies of SNPs at different genomic positions. This
        # is why we group by position and aa_change, not just aa_change.

        # add in a special grouping column to work around the fact that xarray currently
        # doesn't support grouping by multiple variables in the same dimension
        df_grouper = ds_snp_frq[
            ["variant_position", "variant_aa_change"]
        ].to_dataframe()
        grouper_var = df_grouper.apply(
            lambda row: "_".join([str(v) for v in row]), axis="columns"
        )
        ds_snp_frq["variant_position_aa_change"] = "variants", grouper_var

        debug("group by position and amino acid change")
        group_by_aa_change = ds_snp_frq.groupby("variant_position_aa_change")

        debug("apply aggregation")
        ds_aa_frq = group_by_aa_change.map(self._map_snp_to_aa_change_frq_ds)

        debug("add back in cohort variables, unaffected by aggregation")
        cohort_vars = [v for v in ds_snp_frq if v.startswith("cohort_")]
        for v in cohort_vars:
            ds_aa_frq[v] = ds_snp_frq[v]

        debug("sort by genomic position")
        ds_aa_frq = ds_aa_frq.sortby(["variant_position", "variant_aa_change"])

        debug("recompute frequency")
        count = ds_aa_frq["event_count"].values
        nobs = ds_aa_frq["event_nobs"].values
        with np.errstate(divide="ignore", invalid="ignore"):
            frequency = count / nobs  # ignore division warnings
        ds_aa_frq["event_frequency"] = ("variants", "cohorts"), frequency

        debug("recompute max frequency over cohorts")
        with warnings.catch_warnings():
            # ignore "All-NaN slice encountered" warnings
            warnings.simplefilter("ignore", category=RuntimeWarning)
            max_af = np.nanmax(ds_aa_frq["event_frequency"].values, axis=1)
        ds_aa_frq["variant_max_af"] = "variants", max_af

        debug("set up variant dataframe, useful intermediate")
        variant_cols = [v for v in ds_aa_frq if v.startswith("variant_")]
        df_variants = ds_aa_frq[variant_cols].to_dataframe()
        df_variants.columns = [c.split("variant_")[1] for c in df_variants.columns]

        debug("assign new variant label")
        label = self._pandas_apply(
            self._make_snp_label_aa,
            df_variants,
            columns=["aa_change", "contig", "position", "ref_allele", "alt_allele"],
        )
        ds_aa_frq["variant_label"] = "variants", label

        debug("apply variant query if given")
        if variant_query is not None:
            loc_variants = df_variants.eval(variant_query).values
            ds_aa_frq = ds_aa_frq.isel(variants=loc_variants)

        debug("compute new confidence intervals")
        self._add_frequency_ci(ds_aa_frq, ci_method)

        debug("tidy up display by sorting variables")
        ds_aa_frq = ds_aa_frq[sorted(ds_aa_frq)]

        gene_name = self._transcript_to_gene_name(transcript)
        title = transcript
        if gene_name:
            title += f" ({gene_name})"
        title += " SNP frequencies"
        ds_aa_frq.attrs["title"] = title

        return ds_aa_frq

    def _block_jackknife_cohort_diversity_stats(
        self, *, cohort_label, ac, n_jack, confidence_level
    ):
        debug = self._log.debug

        debug("set up for diversity calculations")
        n_sites = ac.shape[0]
        ac = allel.AlleleCountsArray(ac)
        n = ac.sum(axis=1).max()  # number of chromosomes sampled
        n_sites = min(n_sites, ac.shape[0])  # number of sites
        block_length = n_sites // n_jack  # number of sites in each block
        n_sites_j = n_sites - block_length  # number of sites in each jackknife resample

        debug("compute scaling constants")
        a1 = np.sum(1 / np.arange(1, n))
        a2 = np.sum(1 / (np.arange(1, n) ** 2))
        b1 = (n + 1) / (3 * (n - 1))
        b2 = 2 * (n**2 + n + 3) / (9 * n * (n - 1))
        c1 = b1 - (1 / a1)
        c2 = b2 - ((n + 2) / (a1 * n)) + (a2 / (a1**2))
        e1 = c1 / a1
        e2 = c2 / (a1**2 + a2)

        debug(
            "compute some intermediates ahead of time, to minimise computation during jackknife resampling"
        )
        mpd_data = allel.mean_pairwise_difference(ac, fill=0)
        # N.B., here we compute the number of segregating sites as the number
        # of alleles minus 1. This follows the sgkit and tskit implementations,
        # and is different from scikit-allel.
        seg_data = ac.allelism() - 1

        debug("compute estimates from all data")
        theta_pi_abs_data = np.sum(mpd_data)
        theta_pi_data = theta_pi_abs_data / n_sites
        S_data = np.sum(seg_data)
        theta_w_abs_data = S_data / a1
        theta_w_data = theta_w_abs_data / n_sites
        d_data = theta_pi_abs_data - theta_w_abs_data
        d_stdev_data = np.sqrt((e1 * S_data) + (e2 * S_data * (S_data - 1)))
        tajima_d_data = d_data / d_stdev_data

        debug("set up for jackknife resampling")
        jack_theta_pi = []
        jack_theta_w = []
        jack_tajima_d = []

        debug("begin jackknife resampling")
        for i in range(n_jack):
            # locate block to delete
            block_start = i * block_length
            block_stop = block_start + block_length
            loc_j = np.ones(n_sites, dtype=bool)
            loc_j[block_start:block_stop] = False
            assert np.count_nonzero(loc_j) == n_sites_j

            # resample data and compute statistics

            # theta_pi
            mpd_j = mpd_data[loc_j]
            theta_pi_abs_j = np.sum(mpd_j)
            theta_pi_j = theta_pi_abs_j / n_sites_j
            jack_theta_pi.append(theta_pi_j)

            # theta_w
            seg_j = seg_data[loc_j]
            S_j = np.sum(seg_j)
            theta_w_abs_j = S_j / a1
            theta_w_j = theta_w_abs_j / n_sites_j
            jack_theta_w.append(theta_w_j)

            # tajima_d
            d_j = theta_pi_abs_j - theta_w_abs_j
            d_stdev_j = np.sqrt((e1 * S_j) + (e2 * S_j * (S_j - 1)))
            tajima_d_j = d_j / d_stdev_j
            jack_tajima_d.append(tajima_d_j)

        # calculate jackknife stats
        (
            theta_pi_estimate,
            theta_pi_bias,
            theta_pi_std_err,
            theta_pi_ci_err,
            theta_pi_ci_low,
            theta_pi_ci_upp,
        ) = jackknife_ci(
            stat_data=theta_pi_data,
            jack_stat=jack_theta_pi,
            confidence_level=confidence_level,
        )
        (
            theta_w_estimate,
            theta_w_bias,
            theta_w_std_err,
            theta_w_ci_err,
            theta_w_ci_low,
            theta_w_ci_upp,
        ) = jackknife_ci(
            stat_data=theta_w_data,
            jack_stat=jack_theta_w,
            confidence_level=confidence_level,
        )
        (
            tajima_d_estimate,
            tajima_d_bias,
            tajima_d_std_err,
            tajima_d_ci_err,
            tajima_d_ci_low,
            tajima_d_ci_upp,
        ) = jackknife_ci(
            stat_data=tajima_d_data,
            jack_stat=jack_tajima_d,
            confidence_level=confidence_level,
        )

        return dict(
            cohort=cohort_label,
            theta_pi=theta_pi_data,
            theta_pi_estimate=theta_pi_estimate,
            theta_pi_bias=theta_pi_bias,
            theta_pi_std_err=theta_pi_std_err,
            theta_pi_ci_err=theta_pi_ci_err,
            theta_pi_ci_low=theta_pi_ci_low,
            theta_pi_ci_upp=theta_pi_ci_upp,
            theta_w=theta_w_data,
            theta_w_estimate=theta_w_estimate,
            theta_w_bias=theta_w_bias,
            theta_w_std_err=theta_w_std_err,
            theta_w_ci_err=theta_w_ci_err,
            theta_w_ci_low=theta_w_ci_low,
            theta_w_ci_upp=theta_w_ci_upp,
            tajima_d=tajima_d_data,
            tajima_d_estimate=tajima_d_estimate,
            tajima_d_bias=tajima_d_bias,
            tajima_d_std_err=tajima_d_std_err,
            tajima_d_ci_err=tajima_d_ci_err,
            tajima_d_ci_low=tajima_d_ci_low,
            tajima_d_ci_upp=tajima_d_ci_upp,
        )

    @check_types
    @doc(
        summary="""
            Compute genetic diversity summary statistics for a cohort of
            individuals.
        """,
        returns="""
            A pandas series with summary statistics and their confidence
            intervals.
        """,
    )
    def cohort_diversity_stats(
        self,
        cohort: base_params.cohort,
        cohort_size: base_params.cohort_size,
        region: base_params.region,
        site_mask: Optional[base_params.site_mask] = DEFAULT,
        site_class: Optional[base_params.site_class] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        random_seed: base_params.random_seed = 42,
        n_jack: base_params.n_jack = 200,
        confidence_level: base_params.confidence_level = 0.95,
    ) -> pd.Series:
        debug = self._log.debug

        debug("process cohort parameter")
        cohort_query = None
        if isinstance(cohort, str):
            # assume it is one of the predefined cohorts
            cohort_label = cohort
            df_samples = self.sample_metadata(sample_sets=sample_sets)
            cohort_cols = [c for c in df_samples.columns if c.startswith("cohort_")]
            for c in cohort_cols:
                if cohort in set(df_samples[c]):
                    cohort_query = f"{c} == '{cohort}'"
                    break
            if cohort_query is None:
                raise ValueError(f"unknown cohort: {cohort}")

        elif isinstance(cohort, (list, tuple)) and len(cohort) == 2:
            cohort_label, cohort_query = cohort

        else:
            raise TypeError(r"invalid cohort parameter: {cohort!r}")

        debug("access allele counts")
        ac = self.snp_allele_counts(
            region=region,
            site_mask=site_mask,
            site_class=site_class,
            sample_query=cohort_query,
            sample_sets=sample_sets,
            cohort_size=cohort_size,
            random_seed=random_seed,
        )

        debug("compute diversity stats")
        stats = self._block_jackknife_cohort_diversity_stats(
            cohort_label=cohort_label,
            ac=ac,
            n_jack=n_jack,
            confidence_level=confidence_level,
        )

        debug("compute some extra cohort variables")
        df_samples = self.sample_metadata(
            sample_sets=sample_sets, sample_query=cohort_query
        )
        extra_fields = [
            ("taxon", "unique"),
            ("year", "unique"),
            ("month", "unique"),
            ("country", "unique"),
            ("admin1_iso", "unique"),
            ("admin1_name", "unique"),
            ("admin2_name", "unique"),
            ("longitude", "mean"),
            ("latitude", "mean"),
        ]
        for field, agg in extra_fields:
            if agg == "unique":
                vals = df_samples[field].sort_values().unique()
                if len(vals) == 0:
                    val = np.nan
                elif len(vals) == 1:
                    val = vals[0]
                else:
                    val = vals.tolist()
            elif agg == "mean":
                vals = df_samples[field]
                if len(vals) == 0:
                    val = np.nan
                else:
                    val = np.mean(vals)
            else:
                val = np.nan
            stats[field] = val

        return pd.Series(stats)

    @check_types
    @doc(
        summary="""
            Compute genetic diversity summary statistics for multiple cohorts.
        """,
        returns="""
            A DataFrame where each row provides summary statistics and their
            confidence intervals for a single cohort.
        """,
    )
    def diversity_stats(
        self,
        cohorts: base_params.cohorts,
        cohort_size: base_params.cohort_size,
        region: base_params.region,
        site_mask: base_params.site_mask = DEFAULT,
        site_class: Optional[base_params.site_class] = None,
        sample_query: Optional[base_params.sample_query] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        random_seed: base_params.random_seed = 42,
        n_jack: base_params.n_jack = 200,
        confidence_level: base_params.confidence_level = 0.95,
    ) -> pd.DataFrame:
        debug = self._log.debug
        info = self._log.info

        debug("set up cohorts")
        if isinstance(cohorts, dict):
            # user has supplied a custom dictionary mapping cohort identifiers
            # to pandas queries
            cohort_queries = cohorts

        elif isinstance(cohorts, str):
            # user has supplied one of the predefined cohort sets

            df_samples = self.sample_metadata(
                sample_sets=sample_sets, sample_query=sample_query
            )

            # determine column in dataframe - allow abbreviation
            if cohorts.startswith("cohort_"):
                cohorts_col = cohorts
            else:
                cohorts_col = "cohort_" + cohorts
            if cohorts_col not in df_samples.columns:
                raise ValueError(f"{cohorts_col!r} is not a known cohort set")

            # find cohort labels and build queries dictionary
            cohort_labels = sorted(df_samples[cohorts_col].dropna().unique())
            cohort_queries = {coh: f"{cohorts_col} == '{coh}'" for coh in cohort_labels}

        else:
            raise TypeError("cohorts parameter should be dict or str")

        debug("handle sample_query parameter")
        if sample_query is not None:
            cohort_queries = {
                cohort_label: f"({cohort_query}) and ({sample_query})"
                for cohort_label, cohort_query in cohort_queries.items()
            }

        debug("check cohort sizes, drop any cohorts which are too small")
        cohort_queries_checked = dict()
        for cohort_label, cohort_query in cohort_queries.items():
            df_cohort_samples = self.sample_metadata(
                sample_sets=sample_sets, sample_query=cohort_query
            )
            n_samples = len(df_cohort_samples)
            if n_samples < cohort_size:
                info(
                    f"cohort ({cohort_label}) has insufficient samples ({n_samples}) for requested cohort size ({cohort_size}), dropping"  # noqa
                )  # noqa
            else:
                cohort_queries_checked[cohort_label] = cohort_query

        debug("compute diversity stats for cohorts")
        all_stats = []
        for cohort_label, cohort_query in cohort_queries_checked.items():
            stats = self.cohort_diversity_stats(
                cohort=(cohort_label, cohort_query),
                cohort_size=cohort_size,
                region=region,
                site_mask=site_mask,
                site_class=site_class,
                sample_sets=sample_sets,
                random_seed=random_seed,
                n_jack=n_jack,
                confidence_level=confidence_level,
            )
            all_stats.append(stats)
        df_stats = pd.DataFrame(all_stats)

        return df_stats

    @check_types
    @doc(
        summary="""
            Run a Fst genome-wide scan to investigate genetic differentiation
            between two cohorts.
        """,
        returns=dict(
            x="An array containing the window centre point genomic positions",
            fst="An array with Fst statistic values for each window.",
        ),
    )
    def fst_gwss(
        self,
        contig: base_params.contig,
        window_size: fst_params.window_size,
        cohort1_query: base_params.sample_query,
        cohort2_query: base_params.sample_query,
        sample_sets: Optional[base_params.sample_sets] = None,
        site_mask: base_params.site_mask = DEFAULT,
        cohort_size: Optional[base_params.cohort_size] = fst_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = fst_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = fst_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
    ) -> Tuple[np.ndarray, np.ndarray]:
        # TODO could generalise, do this on a region rather than a contig

        # TODO better to support min_cohort_size and max_cohort_size here
        # rather than just a fixed cohort_size

        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._fst_gwss_results_cache_name

        params = dict(
            contig=contig,
            window_size=window_size,
            cohort1_query=cohort1_query,
            cohort2_query=cohort2_query,
            sample_sets=self._prep_sample_sets_param(sample_sets=sample_sets),
            site_mask=self._prep_optional_site_mask_param(site_mask=site_mask),
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        try:
            results = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            results = self._fst_gwss(**params)
            self.results_cache_set(name=name, params=params, results=results)

        x = results["x"]
        fst = results["fst"]

        return x, fst

    def _fst_gwss(
        self,
        contig,
        window_size,
        sample_sets,
        cohort1_query,
        cohort2_query,
        site_mask,
        cohort_size,
        min_cohort_size,
        max_cohort_size,
        random_seed,
    ):
        ds_snps1 = self.snp_calls(
            region=contig,
            sample_query=cohort1_query,
            sample_sets=sample_sets,
            site_mask=site_mask,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        ds_snps2 = self.snp_calls(
            region=contig,
            sample_query=cohort2_query,
            sample_sets=sample_sets,
            site_mask=site_mask,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        gt1 = allel.GenotypeDaskArray(ds_snps1["call_genotype"].data)
        with self._dask_progress(desc="Compute allele counts for cohort 1"):
            ac1 = gt1.count_alleles(max_allele=3).compute()

        gt2 = allel.GenotypeDaskArray(ds_snps2["call_genotype"].data)
        with self._dask_progress(desc="Compute allele counts for cohort 2"):
            ac2 = gt2.count_alleles(max_allele=3).compute()

        pos = ds_snps1["variant_position"].values

        fst = allel.moving_hudson_fst(ac1, ac2, size=window_size)
        x = allel.moving_statistic(pos, statistic=np.mean, size=window_size)

        results = dict(x=x, fst=fst)

        return results

    @check_types
    @doc(
        summary="""
            Plot a heatmap from a pandas DataFrame of frequencies, e.g., output
            from `snp_allele_frequencies()` or `gene_cnv_frequencies()`.
        """,
        parameters=dict(
            df="""
                A DataFrame of frequencies, e.g., output from
                `snp_allele_frequencies()` or `gene_cnv_frequencies()`.
            """,
            index="""
                One or more column headers that are present in the input dataframe.
                This becomes the heatmap y-axis row labels. The column/s must
                produce a unique index.
            """,
            max_len="""
                Displaying large styled dataframes may cause ipython notebooks to
                crash. If the input dataframe is larger than this value, an error
                will be raised.
            """,
            col_width="""
                Plot width per column in pixels (px).
            """,
            row_height="""
                Plot height per row in pixels (px).
            """,
            kwargs="""
                Passed through to `px.imshow()`.
            """,
        ),
        notes="""
            It's recommended to filter the input DataFrame to just rows of interest,
            i.e., fewer rows than `max_len`.
        """,
    )
    def plot_frequencies_heatmap(
        self,
        df: pd.DataFrame,
        index: Union[str, List[str]] = "label",
        max_len: Optional[int] = 100,
        col_width: int = 40,
        row_height: int = 20,
        x_label: plotly_params.x_label = "Cohorts",
        y_label: plotly_params.y_label = "Variants",
        colorbar: plotly_params.colorbar = True,
        width: plotly_params.width = None,
        height: plotly_params.height = None,
        text_auto: plotly_params.text_auto = ".0%",
        aspect: plotly_params.aspect = "auto",
        color_continuous_scale: plotly_params.color_continuous_scale = "Reds",
        title: plotly_params.title = True,
        show: plotly_params.show = True,
        renderer: plotly_params.renderer = None,
        **kwargs,
    ) -> plotly_params.figure:
        debug = self._log.debug

        debug("check len of input")
        if max_len and len(df) > max_len:
            raise ValueError(f"Input DataFrame is longer than {max_len}")

        debug("handle title")
        if title is True:
            title = df.attrs.get("title", None)

        debug("indexing")
        if index is None:
            index = list(df.index.names)
        df = df.reset_index().copy()
        if isinstance(index, list):
            index_col = (
                df[index]
                .astype(str)
                .apply(
                    lambda row: ", ".join([o for o in row if o is not None]),
                    axis="columns",
                )
            )
        elif isinstance(index, str):
            index_col = df[index].astype(str)
        else:
            raise TypeError("wrong type for index parameter, expected list or str")

        debug("check that index is unique")
        if not index_col.is_unique:
            raise ValueError(f"{index} does not produce a unique index")

        debug("drop and re-order columns")
        frq_cols = [col for col in df.columns if col.startswith("frq_")]

        debug("keep only freq cols")
        heatmap_df = df[frq_cols].copy()

        debug("set index")
        heatmap_df.set_index(index_col, inplace=True)

        debug("clean column names")
        heatmap_df.columns = heatmap_df.columns.str.lstrip("frq_")

        debug("deal with width and height")
        if width is None:
            width = 400 + col_width * len(heatmap_df.columns)
            if colorbar:
                width += 40
        if height is None:
            height = 200 + row_height * len(heatmap_df)
            if title is not None:
                height += 40

        debug("plotly heatmap styling")
        fig = px.imshow(
            img=heatmap_df,
            zmin=0,
            zmax=1,
            width=width,
            height=height,
            text_auto=text_auto,
            aspect=aspect,
            color_continuous_scale=color_continuous_scale,
            title=title,
            **kwargs,
        )

        fig.update_xaxes(side="bottom", tickangle=30)
        if x_label is not None:
            fig.update_xaxes(title=x_label)
        if y_label is not None:
            fig.update_yaxes(title=y_label)
        fig.update_layout(
            coloraxis_colorbar=dict(
                title="Frequency",
                tickvals=[0, 0.2, 0.4, 0.6, 0.8, 1.0],
                ticktext=["0%", "20%", "40%", "60%", "80%", "100%"],
            )
        )
        if not colorbar:
            fig.update(layout_coloraxis_showscale=False)

        if show:
            fig.show(renderer=renderer)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Create a time series plot of variant frequencies using plotly.",
        parameters=dict(
            ds="""
                A dataset of variant frequencies, such as returned by
                `snp_allele_frequencies_advanced()`,
                `aa_allele_frequencies_advanced()` or
                `gene_cnv_frequencies_advanced()`.
            """,
            kwargs="Passed through to `px.line()`.",
        ),
        returns="""
            A plotly figure containing line graphs. The resulting figure will
            have one panel per cohort, grouped into columns by taxon, and
            grouped into rows by area. Markers and lines show frequencies of
            variants.
        """,
    )
    def plot_frequencies_time_series(
        self,
        ds: xr.Dataset,
        height: plotly_params.height = None,
        width: plotly_params.width = None,
        title: plotly_params.title = True,
        show: plotly_params.show = True,
        renderer: plotly_params.renderer = None,
        **kwargs,
    ) -> plotly_params.figure:
        debug = self._log.debug

        debug("handle title")
        if title is True:
            title = ds.attrs.get("title", None)

        debug("extract cohorts into a dataframe")
        cohort_vars = [v for v in ds if str(v).startswith("cohort_")]
        df_cohorts = ds[cohort_vars].to_dataframe()
        df_cohorts.columns = [c.split("cohort_")[1] for c in df_cohorts.columns]

        debug("extract variant labels")
        variant_labels = ds["variant_label"].values

        debug("build a long-form dataframe from the dataset")
        dfs = []
        for cohort_index, cohort in enumerate(df_cohorts.itertuples()):
            ds_cohort = ds.isel(cohorts=cohort_index)
            df = pd.DataFrame(
                {
                    "taxon": cohort.taxon,
                    "area": cohort.area,
                    "date": cohort.period_start,
                    "period": str(
                        cohort.period
                    ),  # use string representation for hover label
                    "sample_size": cohort.size,
                    "variant": variant_labels,
                    "count": ds_cohort["event_count"].values,
                    "nobs": ds_cohort["event_nobs"].values,
                    "frequency": ds_cohort["event_frequency"].values,
                    "frequency_ci_low": ds_cohort["event_frequency_ci_low"].values,
                    "frequency_ci_upp": ds_cohort["event_frequency_ci_upp"].values,
                }
            )
            dfs.append(df)
        df_events = pd.concat(dfs, axis=0).reset_index(drop=True)

        debug("remove events with no observations")
        df_events = df_events.query("nobs > 0")

        debug("calculate error bars")
        frq = df_events["frequency"]
        frq_ci_low = df_events["frequency_ci_low"]
        frq_ci_upp = df_events["frequency_ci_upp"]
        df_events["frequency_error"] = frq_ci_upp - frq
        df_events["frequency_error_minus"] = frq - frq_ci_low

        debug("make a plot")
        fig = px.line(
            df_events,
            facet_col="taxon",
            facet_row="area",
            x="date",
            y="frequency",
            error_y="frequency_error",
            error_y_minus="frequency_error_minus",
            color="variant",
            markers=True,
            hover_name="variant",
            hover_data={
                "frequency": ":.0%",
                "period": True,
                "area": True,
                "taxon": True,
                "sample_size": True,
                "date": False,
                "variant": False,
            },
            height=height,
            width=width,
            title=title,
            labels={
                "date": "Date",
                "frequency": "Frequency",
                "variant": "Variant",
                "taxon": "Taxon",
                "area": "Area",
                "period": "Period",
                "sample_size": "Sample size",
            },
            **kwargs,
        )

        debug("tidy plot")
        fig.update_layout(yaxis_range=[-0.05, 1.05])

        if show:
            fig.show(renderer=renderer)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Plot markers on a map showing variant frequencies for cohorts grouped
            by area (space), period (time) and taxon.
        """,
        parameters=dict(
            m="The map on which to add the markers.",
            variant="Index or label of variant to plot.",
            taxon="Taxon to show markers for.",
            period="Time period to show markers for.",
            clear="""
                If True, clear all layers (except the base layer) from the map
                before adding new markers.
            """,
        ),
    )
    def plot_frequencies_map_markers(
        self,
        m,
        ds: frq_params.ds_frequencies_advanced,
        variant: Union[int, str],
        taxon: str,
        period: pd.Period,
        clear: bool = True,
    ):
        debug = self._log.debug
        # only import here because of some problems importing globally
        import ipyleaflet
        import ipywidgets

        debug("slice dataset to variant of interest")
        if isinstance(variant, int):
            ds_variant = ds.isel(variants=variant)
            variant_label = ds["variant_label"].values[variant]
        elif isinstance(variant, str):
            ds_variant = ds.set_index(variants="variant_label").sel(variants=variant)
            variant_label = variant
        else:
            raise TypeError(
                f"Bad type for variant parameter; expected int or str, found {type(variant)}."
            )

        debug("convert to a dataframe for convenience")
        df_markers = ds_variant[
            [
                "cohort_taxon",
                "cohort_area",
                "cohort_period",
                "cohort_lat_mean",
                "cohort_lon_mean",
                "cohort_size",
                "event_frequency",
                "event_frequency_ci_low",
                "event_frequency_ci_upp",
            ]
        ].to_dataframe()

        debug("select data matching taxon and period parameters")
        df_markers = df_markers.loc[
            (
                (df_markers["cohort_taxon"] == taxon)
                & (df_markers["cohort_period"] == period)
            )
        ]

        debug("clear existing layers in the map")
        if clear:
            for layer in m.layers[1:]:
                m.remove_layer(layer)

        debug("add markers")
        for x in df_markers.itertuples():
            marker = ipyleaflet.CircleMarker()
            marker.location = (x.cohort_lat_mean, x.cohort_lon_mean)
            marker.radius = 20
            marker.color = "black"
            marker.weight = 1
            marker.fill_color = "red"
            marker.fill_opacity = x.event_frequency
            popup_html = f"""
                <strong>{variant_label}</strong> <br/>
                Taxon: {x.cohort_taxon} <br/>
                Area: {x.cohort_area} <br/>
                Period: {x.cohort_period} <br/>
                Sample size: {x.cohort_size} <br/>
                Frequency: {x.event_frequency:.0%}
                (95% CI: {x.event_frequency_ci_low:.0%} - {x.event_frequency_ci_upp:.0%})
            """
            marker.popup = ipyleaflet.Popup(
                child=ipywidgets.HTML(popup_html),
            )
            m.add_layer(marker)

    @check_types
    @doc(
        summary="""
            Create an interactive map with markers showing variant frequencies or
            cohorts grouped by area (space), period (time) and taxon.
        """,
        parameters=dict(
            title="""
                If True, attempt to use metadata from input dataset as a plot
                title. Otherwise, use supplied value as a title.
            """,
            epilogue="Additional text to display below the map.",
        ),
        returns="""
            An interactive map with widgets for selecting which variant, taxon
            and time period to display.
        """,
    )
    def plot_frequencies_interactive_map(
        self,
        ds: frq_params.ds_frequencies_advanced,
        center: map_params.center = map_params.center_default,
        zoom: map_params.zoom = map_params.zoom_default,
        title: Union[bool, str] = True,
        epilogue: Union[bool, str] = True,
    ):
        debug = self._log.debug

        import ipyleaflet
        import ipywidgets

        debug("handle title")
        if title is True:
            title = ds.attrs.get("title", None)

        debug("create a map")
        freq_map = ipyleaflet.Map(center=center, zoom=zoom)

        debug("set up interactive controls")
        variants = ds["variant_label"].values
        taxa = np.unique(ds["cohort_taxon"].values)
        periods = np.unique(ds["cohort_period"].values)
        controls = ipywidgets.interactive(
            self.plot_frequencies_map_markers,
            m=ipywidgets.fixed(freq_map),
            ds=ipywidgets.fixed(ds),
            variant=ipywidgets.Dropdown(options=variants, description="Variant: "),
            taxon=ipywidgets.Dropdown(options=taxa, description="Taxon: "),
            period=ipywidgets.Dropdown(options=periods, description="Period: "),
            clear=ipywidgets.fixed(True),
        )

        debug("lay out widgets")
        components = []
        if title is not None:
            components.append(ipywidgets.HTML(value=f"<h3>{title}</h3>"))
        components.append(controls)
        components.append(freq_map)
        if epilogue is True:
            epilogue = """
                Variant frequencies are shown as coloured markers. Opacity of color
                denotes frequency. Click on a marker for more information.
            """
        if epilogue:
            components.append(ipywidgets.HTML(value=f"{epilogue}"))

        out = ipywidgets.VBox(components)

        return out

    @check_types
    @doc(
        summary="""
            Plot sample coordinates from a principal components analysis (PCA)
            as a plotly scatter plot.
        """,
        parameters=dict(
            kwargs="Passed through to `px.scatter()`",
        ),
    )
    def plot_pca_coords(
        self,
        data: pca_params.df_pca,
        x: plotly_params.x = "PC1",
        y: plotly_params.y = "PC2",
        color: plotly_params.color = None,
        symbol: plotly_params.symbol = None,
        jitter_frac: plotly_params.jitter_frac = 0.02,
        random_seed: base_params.random_seed = 42,
        width: plotly_params.width = 900,
        height: plotly_params.height = 600,
        marker_size: plotly_params.marker_size = 10,
        show: plotly_params.show = True,
        renderer: plotly_params.renderer = None,
        **kwargs,
    ) -> plotly_params.figure:
        debug = self._log.debug

        debug(
            "set up data - copy and shuffle so that we don't get systematic over-plotting"
        )
        # TODO does the shuffling actually work?
        data = (
            data.copy().sample(frac=1, random_state=random_seed).reset_index(drop=True)
        )

        debug(
            "apply jitter if desired - helps spread out points when tightly clustered"
        )
        if jitter_frac:
            np.random.seed(random_seed)
            data[x] = jitter(data[x], jitter_frac)
            data[y] = jitter(data[y], jitter_frac)

        debug("convenience variables")
        data["country_location"] = data["country"] + " - " + data["location"]

        debug("set up plotting options")
        hover_data = [
            "partner_sample_id",
            "sample_set",
            "taxon",
            "country",
            "admin1_iso",
            "admin1_name",
            "admin2_name",
            "location",
            "year",
            "month",
        ]
        plot_kwargs = dict(
            width=width,
            height=height,
            color=color,
            symbol=symbol,
            template="simple_white",
            hover_name="sample_id",
            hover_data=hover_data,
            opacity=0.9,
            render_mode="svg",
        )

        debug("special handling for taxon color")
        if color == "taxon":
            self._setup_taxon_colors(plot_kwargs)

        debug("apply any user overrides")
        plot_kwargs.update(kwargs)

        debug("2D scatter plot")
        fig = px.scatter(data, x=x, y=y, **plot_kwargs)

        debug("tidy up")
        fig.update_layout(
            legend=dict(itemsizing="constant"),
        )
        fig.update_traces(marker={"size": marker_size})

        if show:
            fig.show(renderer=renderer)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Plot sample coordinates from a principal components analysis (PCA)
            as a plotly 3D scatter plot.
        """,
        parameters=dict(
            kwargs="Passed through to `px.scatter_3d()`",
        ),
    )
    def plot_pca_coords_3d(
        self,
        data: pca_params.df_pca,
        x: plotly_params.x = "PC1",
        y: plotly_params.y = "PC2",
        z: plotly_params.z = "PC3",
        color: plotly_params.color = None,
        symbol: plotly_params.symbol = None,
        jitter_frac: plotly_params.jitter_frac = 0.02,
        random_seed: base_params.random_seed = 42,
        width: plotly_params.width = 900,
        height: plotly_params.height = 600,
        marker_size: plotly_params.marker_size = 5,
        show: plotly_params.show = True,
        renderer: plotly_params.renderer = None,
        **kwargs,
    ) -> plotly_params.figure:
        debug = self._log.debug

        debug(
            "set up data - copy and shuffle so that we don't get systematic over-plotting"
        )
        # TODO does this actually work?
        data = (
            data.copy().sample(frac=1, random_state=random_seed).reset_index(drop=True)
        )

        debug(
            "apply jitter if desired - helps spread out points when tightly clustered"
        )
        if jitter_frac:
            np.random.seed(random_seed)
            data[x] = jitter(data[x], jitter_frac)
            data[y] = jitter(data[y], jitter_frac)
            data[z] = jitter(data[z], jitter_frac)

        debug("convenience variables")
        data["country_location"] = data["country"] + " - " + data["location"]

        debug("set up plotting options")
        hover_data = [
            "partner_sample_id",
            "sample_set",
            "taxon",
            "country",
            "admin1_iso",
            "admin1_name",
            "admin2_name",
            "location",
            "year",
            "month",
        ]
        plot_kwargs = dict(
            width=width,
            height=height,
            hover_name="sample_id",
            hover_data=hover_data,
            color=color,
            symbol=symbol,
        )

        debug("special handling for taxon color")
        if color == "taxon":
            self._setup_taxon_colors(plot_kwargs)

        debug("apply any user overrides")
        plot_kwargs.update(kwargs)

        debug("3D scatter plot")
        fig = px.scatter_3d(data, x=x, y=y, z=z, **plot_kwargs)

        debug("tidy up")
        fig.update_layout(
            scene=dict(aspectmode="cube"),
            legend=dict(itemsizing="constant"),
        )
        fig.update_traces(marker={"size": marker_size})

        if show:
            fig.show(renderer=renderer)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Plot diversity summary statistics for multiple cohorts.",
        parameters=dict(
            df_stats="Output from `diversity_stats()`.",
            bar_plot_height="Height of bar plots in pixels (px).",
            bar_width="Width per bar in pixels (px).",
            scatter_plot_height="Height of scatter plot in pixels (px).",
            scatter_plot_width="Width of scatter plot in pixels (px).",
            plot_kwargs="Extra plotting parameters.",
        ),
    )
    def plot_diversity_stats(
        self,
        df_stats: pd.DataFrame,
        color: plotly_params.color = None,
        bar_plot_height: int = 450,
        bar_width: int = 30,
        scatter_plot_height: int = 500,
        scatter_plot_width: int = 500,
        template: plotly_params.template = "plotly_white",
        plot_kwargs: Optional[Mapping] = None,
        show: plotly_params.show = True,
        renderer: plotly_params.renderer = None,
    ) -> Optional[Tuple[go.Figure, ...]]:
        debug = self._log.debug

        debug("set up common plotting parameters")
        if plot_kwargs is None:
            plot_kwargs = dict()
        default_plot_kwargs = dict(
            hover_name="cohort",
            hover_data=[
                "taxon",
                "country",
                "admin1_iso",
                "admin1_name",
                "admin2_name",
                "longitude",
                "latitude",
                "year",
                "month",
            ],
            labels={
                "theta_pi_estimate": r"$\widehat{\theta}_{\pi}$",
                "theta_w_estimate": r"$\widehat{\theta}_{w}$",
                "tajima_d_estimate": r"$D$",
                "cohort": "Cohort",
                "taxon": "Taxon",
                "country": "Country",
            },
        )
        if color == "taxon":
            self._setup_taxon_colors(plot_kwargs=default_plot_kwargs)
        default_plot_kwargs.update(plot_kwargs)
        plot_kwargs = default_plot_kwargs
        bar_plot_width = 300 + bar_width * len(df_stats)

        debug("nucleotide diversity bar plot")
        fig1 = px.bar(
            data_frame=df_stats,
            x="cohort",
            y="theta_pi_estimate",
            error_y="theta_pi_ci_err",
            title="Nucleotide diversity",
            color=color,
            height=bar_plot_height,
            width=bar_plot_width,
            template=template,
            **plot_kwargs,
        )

        debug("Watterson's estimator bar plot")
        fig2 = px.bar(
            data_frame=df_stats,
            x="cohort",
            y="theta_w_estimate",
            error_y="theta_w_ci_err",
            title="Watterson's estimator",
            color=color,
            height=bar_plot_height,
            width=bar_plot_width,
            template=template,
            **plot_kwargs,
        )

        debug("Tajima's D bar plot")
        fig3 = px.bar(
            data_frame=df_stats,
            x="cohort",
            y="tajima_d_estimate",
            error_y="tajima_d_ci_err",
            title="Tajima's D",
            color=color,
            height=bar_plot_height,
            width=bar_plot_width,
            template=template,
            **plot_kwargs,
        )

        debug("scatter plot comparing diversity estimators")
        fig4 = px.scatter(
            data_frame=df_stats,
            x="theta_pi_estimate",
            y="theta_w_estimate",
            error_x="theta_pi_ci_err",
            error_y="theta_w_ci_err",
            title="Diversity estimators",
            color=color,
            width=scatter_plot_width,
            height=scatter_plot_height,
            template=template,
            **plot_kwargs,
        )

        if show:
            fig1.show(renderer=renderer)
            fig2.show(renderer=renderer)
            fig3.show(renderer=renderer)
            fig4.show(renderer=renderer)
            return None
        else:
            return (fig1, fig2, fig3, fig4)

    @check_types
    @doc(
        summary="""
            Run and plot a Fst genome-wide scan to investigate genetic
            differentiation between two cohorts.
        """,
    )
    def plot_fst_gwss_track(
        self,
        contig: base_params.contig,
        window_size: fst_params.window_size,
        cohort1_query: base_params.sample_query,
        cohort2_query: base_params.sample_query,
        sample_sets: Optional[base_params.sample_sets] = None,
        site_mask: base_params.site_mask = DEFAULT,
        cohort_size: Optional[base_params.cohort_size] = fst_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = fst_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = fst_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        height: gplt_params.height = 200,
        show: gplt_params.show = True,
        x_range: Optional[gplt_params.x_range] = None,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # compute Fst
        x, fst = self.fst_gwss(
            contig=contig,
            window_size=window_size,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            cohort1_query=cohort1_query,
            cohort2_query=cohort2_query,
            sample_sets=sample_sets,
            site_mask=site_mask,
            random_seed=random_seed,
        )

        # determine X axis range
        x_min = x[0]
        x_max = x[-1]
        if x_range is None:
            x_range = bokeh.models.Range1d(x_min, x_max, bounds="auto")

        # create a figure
        xwheel_zoom = bokeh.models.WheelZoomTool(
            dimensions="width", maintain_focus=False
        )
        if title is None:
            title = f"Cohort 1: {cohort1_query}\nCohort 2: {cohort2_query}"
        fig = bokeh.plotting.figure(
            title=title,
            tools=["xpan", "xzoom_in", "xzoom_out", xwheel_zoom, "reset"],
            active_scroll=xwheel_zoom,
            active_drag="xpan",
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            toolbar_location="above",
            x_range=x_range,
            y_range=(0, 1),
            output_backend=output_backend,
        )

        # plot Fst
        fig.circle(
            x=x,
            y=fst,
            size=3,
            line_width=1,
            line_color="black",
            fill_color=None,
        )

        # tidy up the plot
        fig.yaxis.axis_label = "Fst"
        fig.yaxis.ticker = [0, 1]
        self._bokeh_style_genome_xaxis(fig, contig)

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Run and plot a Fst genome-wide scan to investigate genetic
            differentiation between two cohorts.
        """,
    )
    def plot_fst_gwss(
        self,
        contig: base_params.contig,
        window_size: fst_params.window_size,
        cohort1_query: base_params.sample_query,
        cohort2_query: base_params.sample_query,
        sample_sets: Optional[base_params.sample_sets] = None,
        site_mask: base_params.site_mask = DEFAULT,
        cohort_size: Optional[base_params.cohort_size] = fst_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = fst_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = fst_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        track_height: gplt_params.track_height = 190,
        genes_height: gplt_params.genes_height = gplt_params.genes_height_default,
        show: gplt_params.show = True,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # gwss track
        fig1 = self.plot_fst_gwss_track(
            contig=contig,
            window_size=window_size,
            cohort1_query=cohort1_query,
            cohort2_query=cohort2_query,
            sample_sets=sample_sets,
            site_mask=site_mask,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
            title=title,
            sizing_mode=sizing_mode,
            width=width,
            height=track_height,
            show=False,
            output_backend=output_backend,
        )

        fig1.xaxis.visible = False

        # plot genes
        fig2 = self.plot_genes(
            region=contig,
            sizing_mode=sizing_mode,
            width=width,
            height=genes_height,
            x_range=fig1.x_range,
            show=False,
            output_backend=output_backend,
        )

        # combine plots into a single figure
        fig = bokeh.layouts.gridplot(
            [fig1, fig2],
            ncols=1,
            toolbar_location="above",
            merge_tools=True,
            sizing_mode=sizing_mode,
        )

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Generate h12 GWSS calibration data for different window sizes.",
        returns="""
            A list of H12 calibration run arrays for each window size, containing
            values and percentiles.
        """,
    )
    def h12_calibration(
        self,
        contig: base_params.contig,
        analysis: hap_params.analysis = DEFAULT,
        sample_query: Optional[base_params.sample_query] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        window_sizes: h12_params.window_sizes = h12_params.window_sizes_default,
        random_seed: base_params.random_seed = 42,
    ) -> Mapping[str, np.ndarray]:
        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._h12_calibration_cache_name

        params = dict(
            contig=contig,
            analysis=self._prep_phasing_analysis_param(analysis=analysis),
            window_sizes=window_sizes,
            sample_sets=self._prep_sample_sets_param(sample_sets=sample_sets),
            # N.B., do not be tempted to convert this sample query into integer
            # indices using _prep_sample_selection_params, because the indices
            # are different in the haplotype data.
            sample_query=sample_query,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        try:
            calibration_runs = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            calibration_runs = self._h12_calibration(**params)
            self.results_cache_set(name=name, params=params, results=calibration_runs)

        return calibration_runs

    def _h12_calibration(
        self,
        contig,
        analysis,
        sample_query,
        sample_sets,
        cohort_size,
        min_cohort_size,
        max_cohort_size,
        window_sizes,
        random_seed,
    ) -> Mapping[str, np.ndarray]:
        # access haplotypes
        ds_haps = self.haplotypes(
            region=contig,
            sample_sets=sample_sets,
            sample_query=sample_query,
            analysis=analysis,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        gt = allel.GenotypeDaskArray(ds_haps["call_genotype"].data)
        with self._dask_progress(desc="Load haplotypes"):
            ht = gt.to_haplotypes().compute()

        calibration_runs: Dict[str, np.ndarray] = dict()
        for window_size in self._progress(window_sizes, desc="Compute H12"):
            h1, h12, h123, h2_h1 = allel.moving_garud_h(ht, size=window_size)
            calibration_runs[str(window_size)] = h12

        return calibration_runs

    @check_types
    @doc(
        summary="Plot h12 GWSS calibration data for different window sizes.",
        parameters=dict(
            title="Plot title.",
            show="If True, show the plot.",
        ),
    )
    def plot_h12_calibration(
        self,
        contig: base_params.contig,
        analysis: hap_params.analysis = DEFAULT,
        sample_query: Optional[base_params.sample_query] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        window_sizes: h12_params.window_sizes = h12_params.window_sizes_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[str] = None,
        show: bool = True,
    ) -> gplt_params.figure:
        # get H12 values
        calibration_runs = self.h12_calibration(
            contig=contig,
            analysis=analysis,
            sample_query=sample_query,
            sample_sets=sample_sets,
            window_sizes=window_sizes,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        # compute summaries
        q50 = [np.median(calibration_runs[str(window)]) for window in window_sizes]
        q25 = [
            np.percentile(calibration_runs[str(window)], 25) for window in window_sizes
        ]
        q75 = [
            np.percentile(calibration_runs[str(window)], 75) for window in window_sizes
        ]
        q05 = [
            np.percentile(calibration_runs[str(window)], 5) for window in window_sizes
        ]
        q95 = [
            np.percentile(calibration_runs[str(window)], 95) for window in window_sizes
        ]

        # make plot
        if title is None:
            title = sample_query
        fig = bokeh.plotting.figure(
            title=title,
            width=700,
            height=400,
            x_axis_type="log",
            x_range=bokeh.models.Range1d(window_sizes[0], window_sizes[-1]),
        )
        fig.patch(
            window_sizes + window_sizes[::-1],
            q75 + q25[::-1],
            alpha=0.75,
            line_width=2,
            legend_label="25-75%",
        )
        fig.patch(
            window_sizes + window_sizes[::-1],
            q95 + q05[::-1],
            alpha=0.5,
            line_width=2,
            legend_label="5-95%",
        )
        fig.line(
            window_sizes, q50, line_color="black", line_width=4, legend_label="median"
        )
        fig.circle(window_sizes, q50, color="black", fill_color="black", size=8)

        fig.xaxis.ticker = window_sizes
        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Run h12 genome-wide selection scan.",
        returns=dict(
            x="An array containing the window centre point genomic positions.",
            h12="An array with h12 statistic values for each window.",
        ),
    )
    def h12_gwss(
        self,
        contig: base_params.contig,
        window_size: h12_params.window_size,
        analysis: hap_params.analysis = DEFAULT,
        sample_query: Optional[base_params.sample_query] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
    ) -> Tuple[np.ndarray, np.ndarray]:
        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._h12_gwss_cache_name

        params = dict(
            contig=contig,
            analysis=self._prep_phasing_analysis_param(analysis=analysis),
            window_size=window_size,
            sample_sets=self._prep_sample_sets_param(sample_sets=sample_sets),
            # N.B., do not be tempted to convert this sample query into integer
            # indices using _prep_sample_selection_params, because the indices
            # are different in the haplotype data.
            sample_query=sample_query,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        try:
            results = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            results = self._h12_gwss(**params)
            self.results_cache_set(name=name, params=params, results=results)

        x = results["x"]
        h12 = results["h12"]

        return x, h12

    def _h12_gwss(
        self,
        contig,
        analysis,
        window_size,
        sample_sets,
        sample_query,
        cohort_size,
        min_cohort_size,
        max_cohort_size,
        random_seed,
    ):
        ds_haps = self.haplotypes(
            region=contig,
            analysis=analysis,
            sample_query=sample_query,
            sample_sets=sample_sets,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        gt = allel.GenotypeDaskArray(ds_haps["call_genotype"].data)
        with self._dask_progress(desc="Load haplotypes"):
            ht = gt.to_haplotypes().compute()
        pos = ds_haps["variant_position"].values

        h1, h12, h123, h2_h1 = allel.moving_garud_h(ht, size=window_size)

        x = allel.moving_statistic(pos, statistic=np.mean, size=window_size)

        results = dict(x=x, h12=h12)

        return results

    @check_types
    @doc(
        summary="Plot h12 GWSS data.",
    )
    def plot_h12_gwss_track(
        self,
        contig: base_params.contig,
        window_size: h12_params.window_size,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        height: gplt_params.height = 200,
        show: gplt_params.show = True,
        x_range: Optional[gplt_params.x_range] = None,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # compute H12
        x, h12 = self.h12_gwss(
            contig=contig,
            analysis=analysis,
            window_size=window_size,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            sample_query=sample_query,
            sample_sets=sample_sets,
            random_seed=random_seed,
        )

        # determine X axis range
        x_min = x[0]
        x_max = x[-1]
        if x_range is None:
            x_range = bokeh.models.Range1d(x_min, x_max, bounds="auto")

        # create a figure
        xwheel_zoom = bokeh.models.WheelZoomTool(
            dimensions="width", maintain_focus=False
        )
        if title is None:
            title = sample_query
        fig = bokeh.plotting.figure(
            title=title,
            tools=["xpan", "xzoom_in", "xzoom_out", xwheel_zoom, "reset"],
            active_scroll=xwheel_zoom,
            active_drag="xpan",
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            toolbar_location="above",
            x_range=x_range,
            y_range=(0, 1),
            output_backend=output_backend,
        )

        # plot H12
        fig.circle(
            x=x,
            y=h12,
            size=3,
            line_width=1,
            line_color="black",
            fill_color=None,
        )

        # tidy up the plot
        fig.yaxis.axis_label = "H12"
        fig.yaxis.ticker = [0, 1]
        self._bokeh_style_genome_xaxis(fig, contig)

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Plot h12 GWSS data.",
    )
    def plot_h12_gwss(
        self,
        contig: base_params.contig,
        window_size: h12_params.window_size,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        track_height: gplt_params.track_height = 170,
        genes_height: gplt_params.genes_height = gplt_params.genes_height_default,
        show: gplt_params.show = True,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # gwss track
        fig1 = self.plot_h12_gwss_track(
            contig=contig,
            analysis=analysis,
            window_size=window_size,
            sample_sets=sample_sets,
            sample_query=sample_query,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
            title=title,
            sizing_mode=sizing_mode,
            width=width,
            height=track_height,
            show=False,
            output_backend=output_backend,
        )

        fig1.xaxis.visible = False

        # plot genes
        fig2 = self.plot_genes(
            region=contig,
            sizing_mode=sizing_mode,
            width=width,
            height=genes_height,
            x_range=fig1.x_range,
            show=False,
            output_backend=output_backend,
        )

        # combine plots into a single figure
        fig = bokeh.layouts.gridplot(
            [fig1, fig2],
            ncols=1,
            toolbar_location="above",
            merge_tools=True,
            sizing_mode=sizing_mode,
        )

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Run a H1X genome-wide scan to detect genome regions with
            shared selective sweeps between two cohorts.
        """,
        returns=dict(
            x="An array containing the window centre point genomic positions.",
            h1x="An array with H1X statistic values for each window.",
        ),
    )
    def h1x_gwss(
        self,
        contig: base_params.contig,
        window_size: h12_params.window_size,
        cohort1_query: base_params.sample_query,
        cohort2_query: base_params.sample_query,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
    ) -> Tuple[np.ndarray, np.ndarray]:
        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._h1x_gwss_cache_name

        params = dict(
            contig=contig,
            analysis=self._prep_phasing_analysis_param(analysis=analysis),
            window_size=window_size,
            # N.B., do not be tempted to convert these sample queries into integer
            # indices using _prep_sample_selection_params, because the indices
            # are different in the haplotype data.
            cohort1_query=cohort1_query,
            cohort2_query=cohort2_query,
            sample_sets=self._prep_sample_sets_param(sample_sets=sample_sets),
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        try:
            results = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            results = self._h1x_gwss(**params)
            self.results_cache_set(name=name, params=params, results=results)

        x = results["x"]
        h1x = results["h1x"]

        return x, h1x

    def _h1x_gwss(
        self,
        contig,
        analysis,
        window_size,
        sample_sets,
        cohort1_query,
        cohort2_query,
        cohort_size,
        min_cohort_size,
        max_cohort_size,
        random_seed,
    ):
        # access haplotype datasets for each cohort
        ds1 = self.haplotypes(
            region=contig,
            analysis=analysis,
            sample_query=cohort1_query,
            sample_sets=sample_sets,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )
        ds2 = self.haplotypes(
            region=contig,
            analysis=analysis,
            sample_query=cohort2_query,
            sample_sets=sample_sets,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        # load data into memory
        gt1 = allel.GenotypeDaskArray(ds1["call_genotype"].data)
        with self._dask_progress(desc="Load haplotypes for cohort 1"):
            ht1 = gt1.to_haplotypes().compute()
        gt2 = allel.GenotypeDaskArray(ds2["call_genotype"].data)
        with self._dask_progress(desc="Load haplotypes for cohort 2"):
            ht2 = gt2.to_haplotypes().compute()
        pos = ds1["variant_position"].values

        # run H1X scan
        h1x = _moving_h1x(ht1, ht2, size=window_size)

        # compute window midpoints
        x = allel.moving_statistic(pos, statistic=np.mean, size=window_size)

        results = dict(x=x, h1x=h1x)

        return results

    @check_types
    @doc(
        summary="""
            Run and plot a H1X genome-wide scan to detect genome regions
            with shared selective sweeps between two cohorts.
        """
    )
    def plot_h1x_gwss_track(
        self,
        contig: base_params.contig,
        window_size: h12_params.window_size,
        cohort1_query: base_params.cohort1_query,
        cohort2_query: base_params.cohort2_query,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        height: gplt_params.height = 200,
        show: gplt_params.show = True,
        x_range: Optional[gplt_params.x_range] = None,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # compute H1X
        x, h1x = self.h1x_gwss(
            contig=contig,
            analysis=analysis,
            window_size=window_size,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            cohort1_query=cohort1_query,
            cohort2_query=cohort2_query,
            sample_sets=sample_sets,
            random_seed=random_seed,
        )

        # determine X axis range
        x_min = x[0]
        x_max = x[-1]
        if x_range is None:
            x_range = bokeh.models.Range1d(x_min, x_max, bounds="auto")

        # create a figure
        xwheel_zoom = bokeh.models.WheelZoomTool(
            dimensions="width", maintain_focus=False
        )
        if title is None:
            title = f"Cohort 1: {cohort1_query}\nCohort 2: {cohort2_query}"
        fig = bokeh.plotting.figure(
            title=title,
            tools=["xpan", "xzoom_in", "xzoom_out", xwheel_zoom, "reset"],
            active_scroll=xwheel_zoom,
            active_drag="xpan",
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            toolbar_location="above",
            x_range=x_range,
            y_range=(0, 1),
            output_backend=output_backend,
        )

        # plot H1X
        fig.circle(
            x=x,
            y=h1x,
            size=3,
            line_width=1,
            line_color="black",
            fill_color=None,
        )

        # tidy up the plot
        fig.yaxis.axis_label = "H1X"
        fig.yaxis.ticker = [0, 1]
        self._bokeh_style_genome_xaxis(fig, contig)

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Run and plot a H1X genome-wide scan to detect genome regions
            with shared selective sweeps between two cohorts.
        """
    )
    def plot_h1x_gwss(
        self,
        contig: base_params.contig,
        window_size: h12_params.window_size,
        cohort1_query: base_params.cohort1_query,
        cohort2_query: base_params.cohort2_query,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        cohort_size: Optional[base_params.cohort_size] = h12_params.cohort_size_default,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = h12_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = h12_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        track_height: gplt_params.track_height = 190,
        genes_height: gplt_params.genes_height = gplt_params.genes_height_default,
        show: gplt_params.show = True,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # gwss track
        fig1 = self.plot_h1x_gwss_track(
            contig=contig,
            analysis=analysis,
            window_size=window_size,
            cohort1_query=cohort1_query,
            cohort2_query=cohort2_query,
            sample_sets=sample_sets,
            cohort_size=cohort_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
            title=title,
            sizing_mode=sizing_mode,
            width=width,
            height=track_height,
            show=False,
            output_backend=output_backend,
        )

        fig1.xaxis.visible = False

        # plot genes
        fig2 = self.plot_genes(
            region=contig,
            sizing_mode=sizing_mode,
            width=width,
            height=genes_height,
            x_range=fig1.x_range,
            show=False,
            output_backend=output_backend,
        )

        # combine plots into a single figure
        fig = bokeh.layouts.gridplot(
            [fig1, fig2],
            ncols=1,
            toolbar_location="above",
            merge_tools=True,
            sizing_mode=sizing_mode,
        )

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Run iHS GWSS.",
        returns=dict(
            x="An array containing the window centre point genomic positions.",
            ihs="An array with iHS statistic values for each window.",
        ),
    )
    def ihs_gwss(
        self,
        contig: base_params.contig,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        window_size: ihs_params.window_size = ihs_params.window_size_default,
        percentiles: ihs_params.percentiles = ihs_params.percentiles_default,
        standardize: ihs_params.standardize = True,
        standardization_bins: Optional[ihs_params.standardization_bins] = None,
        standardization_n_bins: ihs_params.standardization_n_bins = ihs_params.standardization_n_bins_default,
        standardization_diagnostics: ihs_params.standardization_diagnostics = False,
        filter_min_maf: ihs_params.filter_min_maf = ihs_params.filter_min_maf_default,
        compute_min_maf: ihs_params.compute_min_maf = ihs_params.compute_min_maf_default,
        min_ehh: ihs_params.min_ehh = ihs_params.min_ehh_default,
        max_gap: ihs_params.max_gap = ihs_params.max_gap_default,
        gap_scale: ihs_params.gap_scale = ihs_params.gap_scale_default,
        include_edges: ihs_params.include_edges = True,
        use_threads: ihs_params.use_threads = True,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = ihs_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = ihs_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
    ) -> Tuple[np.ndarray, np.ndarray]:
        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._ihs_gwss_cache_name

        params = dict(
            contig=contig,
            analysis=self._prep_phasing_analysis_param(analysis=analysis),
            window_size=window_size,
            percentiles=percentiles,
            standardize=standardize,
            standardization_bins=standardization_bins,
            standardization_n_bins=standardization_n_bins,
            standardization_diagnostics=standardization_diagnostics,
            filter_min_maf=filter_min_maf,
            compute_min_maf=compute_min_maf,
            min_ehh=min_ehh,
            include_edges=include_edges,
            max_gap=max_gap,
            gap_scale=gap_scale,
            use_threads=use_threads,
            sample_sets=self._prep_sample_sets_param(sample_sets=sample_sets),
            # N.B., do not be tempted to convert this sample query into integer
            # indices using _prep_sample_selection_params, because the indices
            # are different in the haplotype data.
            sample_query=sample_query,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        try:
            results = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            results = self._ihs_gwss(**params)
            self.results_cache_set(name=name, params=params, results=results)

        x = results["x"]
        ihs = results["ihs"]

        return x, ihs

    def _ihs_gwss(
        self,
        *,
        contig,
        analysis,
        sample_sets,
        sample_query,
        window_size,
        percentiles,
        standardize,
        standardization_bins,
        standardization_n_bins,
        standardization_diagnostics,
        filter_min_maf,
        compute_min_maf,
        min_ehh,
        max_gap,
        gap_scale,
        include_edges,
        use_threads,
        min_cohort_size,
        max_cohort_size,
        random_seed,
    ):
        ds_haps = self.haplotypes(
            region=contig,
            analysis=analysis,
            sample_query=sample_query,
            sample_sets=sample_sets,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        gt = allel.GenotypeDaskArray(ds_haps["call_genotype"].data)
        with self._dask_progress(desc="Load haplotypes"):
            ht = gt.to_haplotypes().compute()

        ac = ht.count_alleles(max_allele=1)
        pos = ds_haps["variant_position"].values

        if filter_min_maf > 0:
            af = ac.to_frequencies()
            maf = np.min(af, axis=1)
            maf_filter = maf > filter_min_maf
            ht = ht.compress(maf_filter, axis=0)
            pos = pos[maf_filter]
            ac = ac[maf_filter]

        # compute iHS
        ihs = allel.ihs(
            h=ht,
            pos=pos,
            min_maf=compute_min_maf,
            min_ehh=min_ehh,
            include_edges=include_edges,
            max_gap=max_gap,
            gap_scale=gap_scale,
            use_threads=use_threads,
        )

        # remove any NaNs
        na_mask = ~np.isnan(ihs)
        ihs = ihs[na_mask]
        pos = pos[na_mask]
        ac = ac[na_mask]

        # take absolute value
        ihs = np.fabs(ihs)

        if standardize:
            ihs, _ = allel.standardize_by_allele_count(
                score=ihs,
                aac=ac[:, 1],
                bins=standardization_bins,
                n_bins=standardization_n_bins,
                diagnostics=standardization_diagnostics,
            )

        if window_size:
            ihs = allel.moving_statistic(
                ihs, statistic=np.percentile, size=window_size, q=percentiles
            )
            pos = allel.moving_statistic(pos, statistic=np.mean, size=window_size)

        results = dict(x=pos, ihs=ihs)

        return results

    @check_types
    @doc(
        summary="Run and plot iHS GWSS data.",
    )
    def plot_ihs_gwss_track(
        self,
        contig: base_params.contig,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        window_size: ihs_params.window_size = ihs_params.window_size_default,
        percentiles: ihs_params.percentiles = ihs_params.percentiles_default,
        standardize: ihs_params.standardize = True,
        standardization_bins: Optional[ihs_params.standardization_bins] = None,
        standardization_n_bins: ihs_params.standardization_n_bins = ihs_params.standardization_n_bins_default,
        standardization_diagnostics: ihs_params.standardization_diagnostics = False,
        filter_min_maf: ihs_params.filter_min_maf = ihs_params.filter_min_maf_default,
        compute_min_maf: ihs_params.compute_min_maf = ihs_params.compute_min_maf_default,
        min_ehh: ihs_params.min_ehh = ihs_params.min_ehh_default,
        max_gap: ihs_params.max_gap = ihs_params.max_gap_default,
        gap_scale: ihs_params.gap_scale = ihs_params.gap_scale_default,
        include_edges: ihs_params.include_edges = True,
        use_threads: ihs_params.use_threads = True,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = ihs_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = ihs_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        palette: ihs_params.palette = ihs_params.palette_default,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        height: gplt_params.height = 200,
        show: gplt_params.show = True,
        x_range: Optional[gplt_params.x_range] = None,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # compute ihs
        x, ihs = self.ihs_gwss(
            contig=contig,
            analysis=analysis,
            window_size=window_size,
            percentiles=percentiles,
            standardize=standardize,
            standardization_bins=standardization_bins,
            standardization_n_bins=standardization_n_bins,
            standardization_diagnostics=standardization_diagnostics,
            filter_min_maf=filter_min_maf,
            compute_min_maf=compute_min_maf,
            min_ehh=min_ehh,
            max_gap=max_gap,
            gap_scale=gap_scale,
            include_edges=include_edges,
            use_threads=use_threads,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            sample_query=sample_query,
            sample_sets=sample_sets,
            random_seed=random_seed,
        )

        # determine X axis range
        x_min = x[0]
        x_max = x[-1]
        if x_range is None:
            x_range = bokeh.models.Range1d(x_min, x_max, bounds="auto")

        # create a figure
        xwheel_zoom = bokeh.models.WheelZoomTool(
            dimensions="width", maintain_focus=False
        )
        if title is None:
            title = sample_query
        fig = bokeh.plotting.figure(
            title=title,
            tools=["xpan", "xzoom_in", "xzoom_out", xwheel_zoom, "reset"],
            active_scroll=xwheel_zoom,
            active_drag="xpan",
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            toolbar_location="above",
            x_range=x_range,
            output_backend=output_backend,
        )

        if window_size:
            if isinstance(percentiles, int):
                percentiles = (percentiles,)
            # Ensure percentiles are sorted so that colors make sense.
            percentiles = tuple(sorted(percentiles))

        # add an empty dimension to ihs array if 1D
        ihs = np.reshape(ihs, (ihs.shape[0], -1))

        # select the base color palette to work from
        base_palette = bokeh.palettes.all_palettes[palette][8]

        # keep only enough colours to plot the IHS tracks
        bokeh_palette = base_palette[: ihs.shape[1]]

        # reverse the colors so darkest is last
        bokeh_palette = bokeh_palette[::-1]

        # plot IHS tracks
        for i in range(ihs.shape[1]):
            ihs_perc = ihs[:, i]
            color = bokeh_palette[i]

            # plot ihs
            fig.circle(
                x=x,
                y=ihs_perc,
                size=4,
                line_width=0,
                line_color=color,
                fill_color=color,
            )

        # tidy up the plot
        fig.yaxis.axis_label = "ihs"
        self._bokeh_style_genome_xaxis(fig, contig)

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Run and plot iHS GWSS data.",
    )
    def plot_ihs_gwss(
        self,
        contig: base_params.contig,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        window_size: ihs_params.window_size = ihs_params.window_size_default,
        percentiles: ihs_params.percentiles = ihs_params.percentiles_default,
        standardize: ihs_params.standardize = True,
        standardization_bins: Optional[ihs_params.standardization_bins] = None,
        standardization_n_bins: ihs_params.standardization_n_bins = ihs_params.standardization_n_bins_default,
        standardization_diagnostics: ihs_params.standardization_diagnostics = False,
        filter_min_maf: ihs_params.filter_min_maf = ihs_params.filter_min_maf_default,
        compute_min_maf: ihs_params.compute_min_maf = ihs_params.compute_min_maf_default,
        min_ehh: ihs_params.min_ehh = ihs_params.min_ehh_default,
        max_gap: ihs_params.max_gap = ihs_params.max_gap_default,
        gap_scale: ihs_params.gap_scale = ihs_params.gap_scale_default,
        include_edges: ihs_params.include_edges = True,
        use_threads: ihs_params.use_threads = True,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = ihs_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = ihs_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        palette: ihs_params.palette = ihs_params.palette_default,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        track_height: gplt_params.track_height = 170,
        genes_height: gplt_params.genes_height = gplt_params.genes_height_default,
        show: gplt_params.show = True,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # gwss track
        fig1 = self.plot_ihs_gwss_track(
            contig=contig,
            analysis=analysis,
            sample_sets=sample_sets,
            sample_query=sample_query,
            window_size=window_size,
            percentiles=percentiles,
            palette=palette,
            standardize=standardize,
            standardization_bins=standardization_bins,
            standardization_n_bins=standardization_n_bins,
            standardization_diagnostics=standardization_diagnostics,
            filter_min_maf=filter_min_maf,
            compute_min_maf=compute_min_maf,
            min_ehh=min_ehh,
            max_gap=max_gap,
            gap_scale=gap_scale,
            include_edges=include_edges,
            use_threads=use_threads,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
            title=title,
            sizing_mode=sizing_mode,
            width=width,
            height=track_height,
            show=False,
            output_backend=output_backend,
        )

        fig1.xaxis.visible = False

        # plot genes
        fig2 = self.plot_genes(
            region=contig,
            sizing_mode=sizing_mode,
            width=width,
            height=genes_height,
            x_range=fig1.x_range,
            show=False,
            output_backend=output_backend,
        )

        # combine plots into a single figure
        fig = bokeh.layouts.gridplot(
            [fig1, fig2],
            ncols=1,
            toolbar_location="above",
            merge_tools=True,
            sizing_mode=sizing_mode,
        )

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    def _garud_g123(self, gt):
        """Compute Garud's G123."""

        # compute diplotype frequencies
        frq_counter = _diplotype_frequencies(gt)

        # convert to array of sorted frequencies
        f = np.sort(np.fromiter(frq_counter.values(), dtype=float))[::-1]

        # compute G123
        g123 = np.sum(f[:3]) ** 2 + np.sum(f[3:] ** 2)

        # These other statistics are not currently needed, but leaving here
        # commented out for future reference...

        # compute G1
        # g1 = np.sum(f**2)

        # compute G12
        # g12 = np.sum(f[:2]) ** 2 + np.sum(f[2:] ** 2)  # type: ignore[index]

        # compute G2/G1
        # g2 = g1 - f[0] ** 2  # type: ignore[index]
        # g2_g1 = g2 / g1

        return g123

    @check_types
    @doc(
        summary="Run a G123 genome-wide selection scan.",
        returns=dict(
            x="An array containing the window centre point genomic positions.",
            g123="An array with G123 statistic values for each window.",
        ),
    )
    def g123_gwss(
        self,
        contig: base_params.contig,
        window_size: g123_params.window_size,
        sites: g123_params.sites = DEFAULT,
        site_mask: base_params.site_mask = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = g123_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = g123_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
    ) -> Tuple[np.ndarray, np.ndarray]:
        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._g123_gwss_cache_name

        if sites == DEFAULT:
            assert self._default_phasing_analysis is not None
            sites = self._default_phasing_analysis
        valid_sites = self.phasing_analysis_ids + ("all", "segregating")
        if sites not in valid_sites:
            raise ValueError(
                f"Invalid value for `sites` parameter, must be one of {valid_sites}."
            )

        params = dict(
            contig=contig,
            sites=sites,
            site_mask=site_mask,
            window_size=window_size,
            sample_sets=self._prep_sample_sets_param(sample_sets=sample_sets),
            # N.B., do not be tempted to convert this sample query into integer
            # indices using _prep_sample_selection_params, because the indices
            # are different in the haplotype data.
            sample_query=sample_query,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        try:
            results = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            results = self._g123_gwss(**params)
            self.results_cache_set(name=name, params=params, results=results)

        x = results["x"]
        g123 = results["g123"]

        return x, g123

    def _g123_gwss(
        self,
        *,
        contig,
        sites,
        site_mask,
        window_size,
        sample_sets,
        sample_query,
        min_cohort_size,
        max_cohort_size,
        random_seed,
    ):
        gt, pos = self._load_data_for_g123(
            contig=contig,
            sites=sites,
            site_mask=site_mask,
            sample_sets=sample_sets,
            sample_query=sample_query,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        g123 = allel.moving_statistic(gt, statistic=self._garud_g123, size=window_size)
        x = allel.moving_statistic(pos, statistic=np.mean, size=window_size)

        results = dict(x=x, g123=g123)

        return results

    @check_types
    @doc(
        summary="Plot G123 GWSS data.",
    )
    def plot_g123_gwss_track(
        self,
        contig: base_params.contig,
        window_size: g123_params.window_size,
        sites: g123_params.sites = DEFAULT,
        site_mask: base_params.site_mask = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = g123_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = g123_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        height: gplt_params.height = 200,
        show: gplt_params.show = True,
        x_range: Optional[gplt_params.x_range] = None,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # compute G123
        x, g123 = self.g123_gwss(
            contig=contig,
            sites=sites,
            site_mask=site_mask,
            window_size=window_size,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            sample_query=sample_query,
            sample_sets=sample_sets,
            random_seed=random_seed,
        )

        # determine X axis range
        x_min = x[0]
        x_max = x[-1]
        if x_range is None:
            x_range = bokeh.models.Range1d(x_min, x_max, bounds="auto")

        # create a figure
        xwheel_zoom = bokeh.models.WheelZoomTool(
            dimensions="width", maintain_focus=False
        )
        if title is None:
            title = sample_query
        fig = bokeh.plotting.figure(
            title=title,
            tools=["xpan", "xzoom_in", "xzoom_out", xwheel_zoom, "reset"],
            active_scroll=xwheel_zoom,
            active_drag="xpan",
            sizing_mode=sizing_mode,
            width=width,
            height=height,
            toolbar_location="above",
            x_range=x_range,
            y_range=(0, 1),
            output_backend=output_backend,
        )

        # plot G123
        fig.circle(
            x=x,
            y=g123,
            size=3,
            line_width=1,
            line_color="black",
            fill_color=None,
        )

        # tidy up the plot
        fig.yaxis.axis_label = "G123"
        fig.yaxis.ticker = [0, 1]
        self._bokeh_style_genome_xaxis(fig, contig)

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="Plot G123 GWSS data.",
    )
    def plot_g123_gwss(
        self,
        contig: base_params.contig,
        window_size: g123_params.window_size,
        sites: g123_params.sites = DEFAULT,
        site_mask: base_params.site_mask = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = g123_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = g123_params.max_cohort_size_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        sizing_mode: gplt_params.sizing_mode = gplt_params.sizing_mode_default,
        width: gplt_params.width = gplt_params.width_default,
        track_height: gplt_params.track_height = 170,
        genes_height: gplt_params.genes_height = gplt_params.genes_height_default,
        show: gplt_params.show = True,
        output_backend: gplt_params.output_backend = gplt_params.output_backend_default,
    ) -> gplt_params.figure:
        # gwss track
        fig1 = self.plot_g123_gwss_track(
            contig=contig,
            sites=sites,
            site_mask=site_mask,
            window_size=window_size,
            sample_sets=sample_sets,
            sample_query=sample_query,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
            title=title,
            sizing_mode=sizing_mode,
            width=width,
            height=track_height,
            show=False,
            output_backend=output_backend,
        )

        fig1.xaxis.visible = False

        # plot genes
        fig2 = self.plot_genes(
            region=contig,
            sizing_mode=sizing_mode,
            width=width,
            height=genes_height,
            x_range=fig1.x_range,
            show=False,
            output_backend=output_backend,
        )

        # combine plots into a single figure
        fig = bokeh.layouts.gridplot(
            [fig1, fig2],
            ncols=1,
            toolbar_location="above",
            merge_tools=True,
            sizing_mode=sizing_mode,
        )

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    def _load_data_for_g123(
        self,
        *,
        contig,
        sites,
        site_mask,
        sample_sets,
        sample_query,
        min_cohort_size,
        max_cohort_size,
        random_seed,
    ):
        debug = self._log.debug
        ds_snps = self.snp_calls(
            region=contig,
            sample_query=sample_query,
            sample_sets=sample_sets,
            site_mask=site_mask,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        gt = allel.GenotypeDaskArray(ds_snps["call_genotype"].data)
        with self._dask_progress(desc="Load genotypes"):
            gt = gt.compute()
        pos = ds_snps["variant_position"].values

        if sites in self.phasing_analysis_ids:
            debug("subsetting to haplotype positions")
            haplotype_pos = self._haplotype_sites_for_contig(
                contig=contig,
                analysis=sites,
                field="POS",
                inline_array=True,
                chunks="native",
            ).compute()
            hap_site_mask = np.in1d(pos, haplotype_pos, assume_unique=True)
            pos = pos[hap_site_mask]
            gt = gt.compress(hap_site_mask, axis=0)

        elif sites == "segregating":
            debug("subsetting to segregating sites")
            ac = gt.count_alleles(max_allele=3)
            seg = ac.is_segregating()
            pos = pos[seg]
            gt = gt.compress(seg, axis=0)

        elif sites == "all":
            debug("using all sites")

        return gt, pos

    @check_types
    @doc(
        summary="Generate g123 GWSS calibration data for different window sizes.",
        returns="""
            A list of g123 calibration run arrays for each window size, containing
            values and percentiles.
        """,
    )
    def g123_calibration(
        self,
        contig: base_params.contig,
        sites: g123_params.sites = DEFAULT,
        site_mask: base_params.site_mask = DEFAULT,
        sample_query: Optional[base_params.sample_query] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = g123_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = g123_params.max_cohort_size_default,
        window_sizes: g123_params.window_sizes = g123_params.window_sizes_default,
        random_seed: base_params.random_seed = 42,
    ) -> Mapping[str, np.ndarray]:
        # change this name if you ever change the behaviour of this function, to
        # invalidate any previously cached data
        name = self._g123_calibration_cache_name

        params = dict(
            contig=contig,
            sites=sites,
            site_mask=self._prep_optional_site_mask_param(site_mask=site_mask),
            window_sizes=window_sizes,
            sample_sets=self._prep_sample_sets_param(sample_sets=sample_sets),
            # N.B., do not be tempted to convert this sample query into integer
            # indices using _prep_sample_selection_params, because the indices
            # are different in the haplotype data.
            sample_query=sample_query,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        try:
            calibration_runs = self.results_cache_get(name=name, params=params)

        except CacheMiss:
            calibration_runs = self._g123_calibration(**params)
            self.results_cache_set(name=name, params=params, results=calibration_runs)

        return calibration_runs

    def _g123_calibration(
        self,
        *,
        contig,
        sites,
        site_mask,
        sample_query,
        sample_sets,
        min_cohort_size,
        max_cohort_size,
        window_sizes,
        random_seed,
    ) -> Mapping[str, np.ndarray]:
        gt, _ = self._load_data_for_g123(
            contig=contig,
            sites=sites,
            site_mask=site_mask,
            sample_query=sample_query,
            sample_sets=sample_sets,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        calibration_runs: Dict[str, np.ndarray] = dict()
        for window_size in self._progress(window_sizes, desc="Compute g123"):
            g123 = allel.moving_statistic(
                gt, statistic=self._garud_g123, size=window_size
            )
            calibration_runs[str(window_size)] = g123

        return calibration_runs

    @check_types
    @doc(
        summary="Plot g123 GWSS calibration data for different window sizes.",
    )
    def plot_g123_calibration(
        self,
        contig: base_params.contig,
        sites: g123_params.sites,
        site_mask: base_params.site_mask = DEFAULT,
        sample_query: Optional[base_params.sample_query] = None,
        sample_sets: Optional[base_params.sample_sets] = None,
        min_cohort_size: Optional[
            base_params.min_cohort_size
        ] = g123_params.min_cohort_size_default,
        max_cohort_size: Optional[
            base_params.max_cohort_size
        ] = g123_params.max_cohort_size_default,
        window_sizes: g123_params.window_sizes = g123_params.window_sizes_default,
        random_seed: base_params.random_seed = 42,
        title: Optional[gplt_params.title] = None,
        show: gplt_params.show = True,
    ) -> gplt_params.figure:
        # get g123 values
        calibration_runs = self.g123_calibration(
            contig=contig,
            sites=sites,
            site_mask=site_mask,
            sample_query=sample_query,
            sample_sets=sample_sets,
            window_sizes=window_sizes,
            min_cohort_size=min_cohort_size,
            max_cohort_size=max_cohort_size,
            random_seed=random_seed,
        )

        # compute summaries
        q50 = [np.median(calibration_runs[str(window)]) for window in window_sizes]
        q25 = [
            np.percentile(calibration_runs[str(window)], 25) for window in window_sizes
        ]
        q75 = [
            np.percentile(calibration_runs[str(window)], 75) for window in window_sizes
        ]
        q05 = [
            np.percentile(calibration_runs[str(window)], 5) for window in window_sizes
        ]
        q95 = [
            np.percentile(calibration_runs[str(window)], 95) for window in window_sizes
        ]

        # make plot
        if title is None:
            title = sample_query
        fig = bokeh.plotting.figure(
            title=title,
            width=700,
            height=400,
            x_axis_type="log",
            x_range=bokeh.models.Range1d(window_sizes[0], window_sizes[-1]),
        )
        fig.patch(
            window_sizes + window_sizes[::-1],
            q75 + q25[::-1],
            alpha=0.75,
            line_width=2,
            legend_label="25-75%",
        )
        fig.patch(
            window_sizes + window_sizes[::-1],
            q95 + q05[::-1],
            alpha=0.5,
            line_width=2,
            legend_label="5-95%",
        )
        fig.line(
            window_sizes, q50, line_color="black", line_width=4, legend_label="median"
        )
        fig.circle(window_sizes, q50, color="black", fill_color="black", size=8)

        fig.xaxis.ticker = window_sizes

        if show:
            bokeh.plotting.show(fig)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Hierarchically cluster haplotypes in region and produce an interactive plot.
        """,
        parameters=dict(
            kwargs="Passed through to `px.scatter()`.",
        ),
    )
    def plot_haplotype_clustering(
        self,
        region: base_params.region,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        color: plotly_params.color = None,
        symbol: plotly_params.symbol = None,
        linkage_method: hapclust_params.linkage_method = hapclust_params.linkage_method_default,
        count_sort: hapclust_params.count_sort = True,
        distance_sort: hapclust_params.distance_sort = False,
        cohort_size: Optional[base_params.cohort_size] = None,
        random_seed: base_params.random_seed = 42,
        width: plotly_params.width = 1000,
        height: plotly_params.height = 500,
        show: plotly_params.show = True,
        renderer: plotly_params.renderer = None,
        **kwargs,
    ) -> plotly_params.figure:
        from scipy.cluster.hierarchy import linkage

        from .plotly_dendrogram import create_dendrogram

        debug = self._log.debug

        ds_haps = self.haplotypes(
            region=region,
            analysis=analysis,
            sample_query=sample_query,
            sample_sets=sample_sets,
            cohort_size=cohort_size,
            random_seed=random_seed,
        )

        gt = allel.GenotypeDaskArray(ds_haps["call_genotype"].data)
        with self._dask_progress(desc="Load haplotypes"):
            ht = gt.to_haplotypes().compute()

        debug("load sample metadata")
        df_samples = self.sample_metadata(
            sample_sets=sample_sets, sample_query=sample_query
        )
        debug("align sample metadata with haplotypes")
        phased_samples = ds_haps["sample_id"].values.tolist()
        df_samples_phased = (
            df_samples.set_index("sample_id").loc[phased_samples].reset_index()
        )

        debug("set up plotting options")
        hover_data = [
            "sample_id",
            "partner_sample_id",
            "sample_set",
            "taxon",
            "country",
            "admin1_iso",
            "admin1_name",
            "admin2_name",
            "location",
            "year",
            "month",
        ]

        if color and color not in hover_data:
            hover_data.append(color)
        if symbol and symbol not in hover_data:
            hover_data.append(symbol)

        plot_kwargs = dict(
            template="simple_white",
            hover_name="sample_id",
            hover_data=hover_data,
            render_mode="svg",
        )

        debug("special handling for taxon color")
        if color == "taxon":
            self._setup_taxon_colors(plot_kwargs)

        debug("apply any user overrides")
        plot_kwargs.update(kwargs)

        debug("Create dendrogram with plotly")
        # set labels as the index which we extract to reorder metadata
        leaf_labels = np.arange(ht.shape[1])
        # get the max distance, required to set xmin, xmax, which we need xmin to be slightly below 0
        max_dist = _get_max_hamming_distance(
            ht.T, metric="hamming", linkage_method=linkage_method
        )
        # noinspection PyTypeChecker
        fig = create_dendrogram(
            ht.T,
            distfun=lambda x: _hamming_to_snps(x),
            linkagefun=lambda x: linkage(x, method=linkage_method),
            labels=leaf_labels,
            color_threshold=0,
            count_sort=count_sort,
            distance_sort=distance_sort,
        )
        fig.update_traces(
            hoverinfo="y",
            line=dict(width=0.5, color="black"),
        )

        title_lines = []
        if sample_sets is not None:
            title_lines.append(f"sample sets: {sample_sets}")
        if sample_query is not None:
            title_lines.append(f"sample query: {sample_query}")
        title_lines.append(f"genomic region: {region} ({ht.shape[0]} SNPs)")
        title = "<br>".join(title_lines)

        fig.update_layout(
            width=width,
            height=height,
            title=title,
            autosize=True,
            hovermode="closest",
            plot_bgcolor="white",
            yaxis_title="Distance (no. SNPs)",
            xaxis_title="Haplotypes",
            showlegend=True,
        )

        # Repeat the dataframe so there is one row of metadata for each haplotype
        df_samples_phased_haps = pd.DataFrame(
            np.repeat(df_samples_phased.values, 2, axis=0)
        )
        df_samples_phased_haps.columns = df_samples_phased.columns
        # select only columns in hover_data
        df_samples_phased_haps = df_samples_phased_haps[hover_data]
        debug("Reorder haplotype metadata to align with haplotype clustering")
        df_samples_phased_haps = df_samples_phased_haps.loc[
            fig.layout.xaxis["ticktext"]
        ]
        fig.update_xaxes(mirror=False, showgrid=True, showticklabels=False, ticks="")
        fig.update_yaxes(
            mirror=False, showgrid=True, showline=True, range=[-2, max_dist + 1]
        )

        debug("Add scatter plot with hover text")
        fig.add_traces(
            list(
                px.scatter(
                    df_samples_phased_haps,
                    x=fig.layout.xaxis["tickvals"],
                    y=np.repeat(-1, len(ht.T)),
                    color=color,
                    symbol=symbol,
                    **plot_kwargs,
                ).select_traces()
            )
        )

        if show:
            fig.show(renderer=renderer)
            return None
        else:
            return fig

    @check_types
    @doc(
        summary="""
            Construct a median-joining haplotype network and display it using
            Cytoscape.
        """,
        extended_summary="""
            A haplotype network provides a visualisation of the genetic distance
            between haplotype_ Each node in the network represents a unique
            haplotype. The size (area) of the node is scaled by the number of
            times that unique haplotype was observed within the selected samples.
            A connection between two nodes represents a single SNP difference
            between the corresponding haplotypes.
        """,
    )
    def plot_haplotype_network(
        self,
        region: base_params.region,
        analysis: hap_params.analysis = DEFAULT,
        sample_sets: Optional[base_params.sample_sets] = None,
        sample_query: Optional[base_params.sample_query] = None,
        max_dist: hapnet_params.max_dist = hapnet_params.max_dist_default,
        color: Optional[hapnet_params.color] = None,
        color_discrete_sequence: Optional[hapnet_params.color_discrete_sequence] = None,
        color_discrete_map: Optional[hapnet_params.color_discrete_map] = None,
        category_orders: Optional[hapnet_params.category_order] = None,
        node_size_factor: hapnet_params.node_size_factor = hapnet_params.node_size_factor_default,
        layout: hapnet_params.layout = hapnet_params.layout_default,
        layout_params: Optional[hapnet_params.layout_params] = None,
        server_port: Optional[dash_params.server_port] = None,
        server_mode: Optional[
            dash_params.server_mode
        ] = dash_params.server_mode_default,
        height: dash_params.height = 650,
        width: Optional[dash_params.width] = "100%",
    ):
        import dash_cytoscape as cyto
        from dash import dcc, html
        from dash.dependencies import Input, Output
        from jupyter_dash import JupyterDash

        if layout != "cose":
            cyto.load_extra_layouts()
        # leave this for user code, if needed (doesn't seem necessary on colab)
        # JupyterDash.infer_jupyter_proxy_config()

        debug = self._log.debug

        debug("access haplotypes dataset")
        ds_haps = self.haplotypes(
            region=region,
            sample_sets=sample_sets,
            sample_query=sample_query,
            analysis=analysis,
        )

        debug("access sample metadata")
        df_samples = self.sample_metadata(
            sample_query=sample_query, sample_sets=sample_sets
        )

        debug("setup haplotype metadata")
        samples_phased = ds_haps["sample_id"].values
        df_samples_phased = (
            df_samples.set_index("sample_id").loc[samples_phased].reset_index()
        )
        df_haps = df_samples_phased.loc[df_samples_phased.index.repeat(2)].reset_index(
            drop=True
        )

        debug("load haplotypes")
        gt = allel.GenotypeDaskArray(ds_haps["call_genotype"].data)
        with self._dask_progress(desc="Load haplotypes"):
            ht = gt.to_haplotypes().compute()

        debug("count alleles and select segregating sites")
        ac = gt.count_alleles(max_allele=1)
        loc_seg = ac.is_segregating()
        ht_seg = ht[loc_seg]

        debug("identify distinct haplotypes")
        ht_distinct_sets = ht_seg.distinct()
        # find indices of distinct haplotypes - just need one per set
        ht_distinct_indices = [min(s) for s in ht_distinct_sets]
        # reorder by index - TODO is this necessary?
        ix = np.argsort(ht_distinct_indices)
        ht_distinct_indices = [ht_distinct_indices[i] for i in ix]
        ht_distinct_sets = [ht_distinct_sets[i] for i in ix]
        # obtain an array of distinct haplotypes
        ht_distinct = ht_seg.take(ht_distinct_indices, axis=1)
        # count how many observations per distinct haplotype
        ht_counts = [len(s) for s in ht_distinct_sets]

        debug("construct median joining network")
        ht_distinct_mjn, edges, alt_edges = median_joining_network(
            ht_distinct, max_dist=max_dist
        )
        edges = np.triu(edges)
        alt_edges = np.triu(alt_edges)

        debug("setup colors")
        color_values = None
        color_values_display = None
        color_discrete_map_display = None
        ht_color_counts = None
        if color is not None:
            # sanitise color column - necessary to avoid grey pie chart segments
            df_haps["partition"] = df_haps[color].str.replace(r"\W", "", regex=True)

            # extract all unique values of the color column
            color_values = df_haps["partition"].unique()
            color_values_mapping = dict(zip(df_haps["partition"], df_haps[color]))
            color_values_display = [color_values_mapping[c] for c in color_values]

            # count color values for each distinct haplotype
            ht_color_counts = [
                df_haps.iloc[list(s)]["partition"].value_counts().to_dict()
                for s in ht_distinct_sets
            ]

            if color == "taxon":
                # special case, standardise taxon colors and order
                color_params = self._setup_taxon_colors()
                color_discrete_map = color_params["color_discrete_map"]
                color_discrete_map_display = color_discrete_map
                category_orders = color_params["category_orders"]

            elif color_discrete_map is None:
                # set up a color palette
                if color_discrete_sequence is None:
                    if len(color_values) <= 10:
                        color_discrete_sequence = px.colors.qualitative.Plotly
                    else:
                        color_discrete_sequence = px.colors.qualitative.Alphabet

                # map values to colors
                color_discrete_map = {
                    v: c for v, c in zip(color_values, cycle(color_discrete_sequence))
                }
                color_discrete_map_display = {
                    v: c
                    for v, c in zip(
                        color_values_display, cycle(color_discrete_sequence)
                    )
                }

        debug("construct graph")
        anon_width = np.sqrt(0.3 * node_size_factor)
        graph_nodes, graph_edges = mjn_graph(
            ht_distinct=ht_distinct,
            ht_distinct_mjn=ht_distinct_mjn,
            ht_counts=ht_counts,
            ht_color_counts=ht_color_counts,
            color=color,
            color_values=color_values,
            edges=edges,
            alt_edges=alt_edges,
            node_size_factor=node_size_factor,
            anon_width=anon_width,
        )

        debug("prepare graph data for cytoscape")
        elements = [{"data": n} for n in graph_nodes] + [
            {"data": e} for e in graph_edges
        ]

        debug("define node style")
        node_style = {
            "width": "data(width)",
            "height": "data(width)",
            "pie-size": "100%",
        }
        if color and color_discrete_map is not None:
            # here are the styles which control the display of nodes as pie
            # charts
            for i, (v, c) in enumerate(color_discrete_map.items()):
                node_style[f"pie-{i + 1}-background-color"] = c
                node_style[
                    f"pie-{i + 1}-background-size"
                ] = f"mapData({v}, 0, 100, 0, 100)"
        node_stylesheet = {
            "selector": "node",
            "style": node_style,
        }
        debug(node_stylesheet)

        debug("define edge style")
        edge_stylesheet = {
            "selector": "edge",
            "style": {"curve-style": "bezier", "width": 2, "opacity": 0.5},
        }

        debug("define style for selected node")
        selected_stylesheet = {
            "selector": ":selected",
            "style": {
                "border-width": "3px",
                "border-style": "solid",
                "border-color": "black",
            },
        }

        debug("create figure legend")
        if color is not None:
            legend_fig = plotly_discrete_legend(
                color=color,
                color_values=color_values_display,
                color_discrete_map=color_discrete_map_display,
                category_orders=category_orders,
            )
            legend_component = dcc.Graph(
                id="legend",
                figure=legend_fig,
                config=dict(
                    displayModeBar=False,
                ),
            )
        else:
            legend_component = html.Div()

        debug("define cytoscape component")
        if layout_params is None:
            graph_layout_params = dict()
        else:
            graph_layout_params = dict(**layout_params)
        graph_layout_params["name"] = layout
        graph_layout_params.setdefault("padding", 10)
        graph_layout_params.setdefault("animate", False)

        cytoscape_component = cyto.Cytoscape(
            id="cytoscape",
            elements=elements,
            layout=graph_layout_params,
            stylesheet=[
                node_stylesheet,
                edge_stylesheet,
                selected_stylesheet,
            ],
            style={
                # width and height needed to get cytoscape component to display
                "width": "100%",
                "height": "100%",
                "background-color": "white",
            },
            # enable selecting multiple nodes with shift click and drag
            boxSelectionEnabled=True,
            # prevent accidentally zooming out to oblivion
            minZoom=0.1,
        )

        debug("create dash app")
        app = JupyterDash(
            "dash-cytoscape-network",
            # this stylesheet is used to provide support for a rows and columns
            # layout of the components
            external_stylesheets=["https://codepen.io/chriddyp/pen/bWLwgP.css"],
        )
        # this is an optimisation, it's generally faster to serve script files from CDN
        app.scripts.config.serve_locally = False
        app.layout = html.Div(
            [
                html.Div(
                    cytoscape_component,
                    className="nine columns",
                    style={
                        # required to get cytoscape component to show ...
                        # multiply by factor <1 to prevent scroll overflow
                        "height": f"{height * .93}px",
                        "border": "1px solid black",
                    },
                ),
                html.Div(
                    legend_component,
                    className="three columns",
                    style={
                        "height": f"{height * .93}px",
                    },
                ),
                html.Div(id="output"),
            ],
        )

        debug(
            "define a callback function to display information about the selected node"
        )

        @app.callback(Output("output", "children"), Input("cytoscape", "tapNodeData"))
        def display_tap_node_data(data):
            if data is None:
                return "Click or tap a node for more information."
            else:
                n = data["count"]
                text = f"No. haplotypes: {n}"
                selected_color_data = {
                    color_v_display: int(data.get(color_v, 0) * n / 100)
                    for color_v, color_v_display in zip(
                        color_values, color_values_display
                    )
                }
                selected_color_data = sorted(
                    selected_color_data.items(), key=lambda item: item[1], reverse=True
                )
                color_texts = [
                    f"{color_v}: {color_n}"
                    for color_v, color_n in selected_color_data
                    if color_n > 0
                ]
                if color_texts:
                    color_texts = "; ".join(color_texts)
                    text += f" ({color_texts})"
                return text

        debug("set up run parameters")
        # workaround weird mypy bug here
        run_params: Dict[str, Any] = dict()
        if height is not None:
            run_params["height"] = height
        if width is not None:
            run_params["width"] = width
        if server_port is not None:
            run_params["port"] = server_port
        if server_mode is not None:
            run_params["mode"] = server_mode

        debug("launch the dash app")
        # TODO I don't think this actually returns anything
        return app.run_server(**run_params)


def _hamming_to_snps(h):
    """
    Cluster haplotype array and return the number of SNP differences
    """
    from scipy.spatial.distance import pdist

    dist = pdist(h, metric="hamming")
    dist *= h.shape[1]
    return dist


def _get_max_hamming_distance(h, metric="hamming", linkage_method="single"):
    """
    Find the maximum hamming distance between haplotypes
    """
    from scipy.cluster.hierarchy import linkage

    z = linkage(h, metric=metric, method=linkage_method)

    # Get the distances column
    dists = z[:, 2]
    # Convert to the number of SNP differences
    dists *= h.shape[1]
    # Return the maximum
    return dists.max()


def _diplotype_frequencies(gt):
    """Compute diplotype frequencies, returning a dictionary that maps
    diplotype hash values to frequencies."""
    # TODO could use faster hashing
    n = gt.shape[1]
    hashes = [hash(gt[:, i].tobytes()) for i in range(n)]
    counts = Counter(hashes)
    freqs = {key: count / n for key, count in counts.items()}
    return freqs


def _haplotype_frequencies(h):
    """Compute haplotype frequencies, returning a dictionary that maps
    haplotype hash values to frequencies."""
    # TODO could use faster hashing
    n = h.shape[1]
    hashes = [hash(h[:, i].tobytes()) for i in range(n)]
    counts = Counter(hashes)
    freqs = {key: count / n for key, count in counts.items()}
    return freqs


def _haplotype_joint_frequencies(ha, hb):
    """Compute the joint frequency of haplotypes in two difference
    cohorts. Returns a dictionary mapping haplotype hash values to
    the product of frequencies in each cohort."""
    frqa = _haplotype_frequencies(ha)
    frqb = _haplotype_frequencies(hb)
    keys = set(frqa.keys()) | set(frqb.keys())
    joint_freqs = {key: frqa.get(key, 0) * frqb.get(key, 0) for key in keys}
    return joint_freqs


def _h1x(ha, hb):
    """Compute H1X, the sum of joint haplotype frequencies between
    two cohorts, which is a summary statistic useful for detecting
    shared selective sweeps."""
    jf = _haplotype_joint_frequencies(ha, hb)
    return np.sum(list(jf.values()))


def _moving_h1x(ha, hb, size, start=0, stop=None, step=None):
    """Compute H1X in moving windows.

    Parameters
    ----------
    ha : array_like, int, shape (n_variants, n_haplotypes)
        Haplotype array for the first cohort.
    hb : array_like, int, shape (n_variants, n_haplotypes)
        Haplotype array for the second cohort.
    size : int
        The window size (number of variants).
    start : int, optional
        The index at which to start.
    stop : int, optional
        The index at which to stop.
    step : int, optional
        The number of variants between start positions of windows. If not
        given, defaults to the window size, i.e., non-overlapping windows.

    Returns
    -------
    h1x : ndarray, float, shape (n_windows,)
        H1X values (sum of squares of joint haplotype frequencies).
    """

    assert ha.ndim == hb.ndim == 2
    assert ha.shape[0] == hb.shape[0]

    # construct moving windows
    windows = allel.index_windows(ha, size, start, stop, step)

    # compute statistics for each window
    out = np.array([_h1x(ha[i:j], hb[i:j]) for i, j in windows])

    return out
