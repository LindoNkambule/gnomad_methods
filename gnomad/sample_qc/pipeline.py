# noqa: D100

import functools
import logging
import operator

import hail as hl
from gnomad.sample_qc.sex import get_ploidy_cutoffs, get_sex_expr
from gnomad.utils.annotations import bi_allelic_site_inbreeding_expr, bi_allelic_expr
from gnomad.utils.filtering import filter_low_conf_regions, filter_to_adj
from gnomad.utils.reference_genome import get_reference_genome
from gnomad.utils.sparse_mt import impute_sex_ploidy

from typing import List, Optional, Union

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def filter_rows_for_qc(
    mt: hl.MatrixTable,
    min_af: Optional[float] = 0.001,
    min_callrate: Optional[float] = 0.99,
    min_inbreeding_coeff_threshold: Optional[float] = -0.8,
    min_hardy_weinberg_threshold: Optional[float] = 1e-8,
    apply_hard_filters: bool = True,
    bi_allelic_only: bool = True,
    snv_only: bool = True,
) -> hl.MatrixTable:
    """
    Annotate rows with `sites_callrate`, `site_inbreeding_coeff` and `af`, then apply thresholds.

    AF and callrate thresholds are taken from gnomAD QC; inbreeding coeff, MQ, FS and QD filters are taken from
    GATK best practices.

    .. note::

        This function expect the typical ``info`` annotation of type struct with fields ``MQ``, ``FS`` and ``QD``
        if applying hard filters.

    :param mt: Input MT
    :param min_af: Minimum site AF to keep. Not applied if set to ``None``.
    :param min_callrate: Minimum site call rate to keep. Not applied if set to ``None``.
    :param min_inbreeding_coeff_threshold: Minimum site inbreeding coefficient to keep. Not applied if set to ``None``.
    :param min_hardy_weinberg_threshold: Minimum site HW test p-value to keep. Not applied if set to ``None``.
    :paramapply_hard_filters: Whether to apply standard GAKT default site hard filters: QD >= 2, FS <= 60 and MQ >= 30
    :parambi_allelic_only: Whether to only keep bi-allelic sites or include multi-allelic sites too
    :paramsnv_only: Whether to only keep SNVs or include other variant types
    :return: annotated and filtered table
    """
    annotation_expr = {}

    if min_af is not None:
        annotation_expr["af"] = hl.agg.mean(mt.GT.n_alt_alleles()) / 2
    if min_callrate is not None:
        annotation_expr["site_callrate"] = hl.agg.fraction(hl.is_defined(mt.GT))
    if min_inbreeding_coeff_threshold is not None:
        annotation_expr["site_inbreeding_coeff"] = bi_allelic_site_inbreeding_expr(
            mt.GT
        )
    if min_hardy_weinberg_threshold is not None:
        annotation_expr["hwe"] = hl.agg.hardy_weinberg_test(mt.GT)

    if annotation_expr:
        mt = mt.annotate_rows(**annotation_expr)

    filter_expr = []
    if min_af is not None:
        filter_expr.append((mt.af > min_af))
    if min_callrate is not None:
        filter_expr.append((mt.site_callrate > min_callrate))
    if min_inbreeding_coeff_threshold is not None:
        filter_expr.append((mt.site_inbreeding_coeff > min_inbreeding_coeff_threshold))
    if min_hardy_weinberg_threshold is not None:
        filter_expr.append((mt.hwe.p_value > min_hardy_weinberg_threshold))
    if snv_only:
        filter_expr.append(hl.is_snp(mt.alleles[0], mt.alleles[1]))
    if bi_allelic_only:
        filter_expr.append(bi_allelic_expr(mt))

    if apply_hard_filters:
        if "info" in mt.row_value:
            if "QD" in mt.info:
                filter_expr.append((mt.info.QD >= 2))
            else:
                logger.warning(
                    "Could not apply QD hard filter, as `info.QD` not found in schema."
                )
            if "FS" in mt.info:
                filter_expr.append((mt.info.FS <= 60))
            else:
                logger.warning(
                    "Could not apply FS hard filter, as `info.FS` not found in schema."
                )
            if "MQ" in mt.info:
                filter_expr.append((mt.info.MQ >= 30))
            else:
                logger.warning(
                    "Could not apply MQ hard filter, as `info.MQ` not found in schema."
                )
        else:
            logger.warning(
                "Could not apply hard filters as `info` not found in schema."
            )

    return mt.filter_rows(functools.reduce(operator.iand, filter_expr))


def get_qc_mt(
    mt: hl.MatrixTable,
    adj_only: bool = True,
    min_af: Optional[float] = 0.001,
    min_callrate: Optional[float] = 0.99,
    min_inbreeding_coeff_threshold: Optional[float] = -0.8,
    min_hardy_weinberg_threshold: Optional[float] = 1e-8,
    apply_hard_filters: bool = True,
    ld_r2: Optional[float] = 0.1,
    filter_lcr: bool = True,
    filter_decoy: bool = True,
    filter_segdup: bool = True,
    filter_exome_low_coverage_regions: bool = False,
    high_conf_regions: Optional[List[str]] = None,
    checkpoint_path: Optional[str] = None,
) -> hl.MatrixTable:
    """
    Create a QC-ready MT.

    Keeps the following:
        - Variants outside known problematic regions
        - Bi-allelic SNVs only
        - Variants passing hard thresholds
        - Variants passing the set call rate and MAF thresholds
        - Genotypes passing on gnomAD ADJ criteria (GQ>=20, DP>=10, AB>0.2 for hets)

    In addition, the MT will be LD-pruned if `ld_r2` is set.

    :param mt: Input MT
    :param adj_only: If set, only ADJ genotypes are kept. This filter is applied before the call rate and AF calculation.
    :param min_af: Minimum allele frequency to keep. Not applied if set to ``None``.
    :param min_callrate: Minimum call rate to keep. Not applied if set to ``None``.
    :param min_inbreeding_coeff_threshold: Minimum site inbreeding coefficient to keep. Not applied if set to ``None``.
    :param min_hardy_weinberg_threshold: Minimum site HW test p-value to keep. Not applied if set to ``None``.
    :param apply_hard_filters: Whether to apply standard GAKT default site hard filters: QD >= 2, FS <= 60 and MQ >= 30
    :param ld_r2: Minimum r2 to keep when LD-pruning (set to `None` for no LD pruning)
    :param filter_lcr: Filter LCR regions
    :param filter_decoy: Filter decoy regions
    :param filter_segdup: Filter segmental duplication regions
    :param filter_exome_low_coverage_regions: If set, only high coverage exome regions (computed from gnomAD are kept)
    :param high_conf_regions: If given, the data will be filtered to only include variants in those regions
    :param checkpoint_path: If given, the QC MT will be checkpointed to the specified path before running LD pruning. If not specified, persist will be used instead.
    :return: Filtered MT
    """
    logger.info("Creating QC MatrixTable")
    if ld_r2 is not None:
        logger.warning(
            "The LD-prune step of this function requires non-preemptible workers only!"
        )

    qc_mt = filter_low_conf_regions(
        mt,
        filter_lcr=filter_lcr,
        filter_decoy=filter_decoy,
        filter_segdup=filter_segdup,
        filter_exome_low_coverage_regions=filter_exome_low_coverage_regions,
        high_conf_regions=high_conf_regions,
    )

    if adj_only:
        qc_mt = filter_to_adj(
            qc_mt
        )  # TODO: Make sure that this works fine before call rate filtering

    qc_mt = filter_rows_for_qc(
        qc_mt,
        min_af,
        min_callrate,
        min_inbreeding_coeff_threshold,
        min_hardy_weinberg_threshold,
        apply_hard_filters,
    )

    if ld_r2 is not None:
        if checkpoint_path:
            logger.info("Checkpointing the MT and LD pruning")
            qc_mt = qc_mt.checkpoint(checkpoint_path, overwrite=True)
        else:
            logger.info("Persisting the MT and LD pruning")
            qc_mt = qc_mt.persist()
        unfiltered_qc_mt = qc_mt.unfilter_entries()
        pruned_ht = hl.ld_prune(unfiltered_qc_mt.GT, r2=ld_r2)
        qc_mt = qc_mt.filter_rows(hl.is_defined(pruned_ht[qc_mt.row_key]))

    qc_mt = qc_mt.annotate_globals(
        qc_mt_params=hl.struct(
            adj_only=adj_only,
            min_af=min_af if min_af is not None else hl.null(hl.tfloat32),
            min_callrate=min_callrate
            if min_callrate is not None
            else hl.null(hl.tfloat32),
            inbreeding_coeff_threshold=min_inbreeding_coeff_threshold
            if min_inbreeding_coeff_threshold is not None
            else hl.null(hl.tfloat32),
            min_hardy_weinberg_threshold=min_hardy_weinberg_threshold
            if min_hardy_weinberg_threshold is not None
            else hl.null(hl.tfloat32),
            apply_hard_filters=apply_hard_filters,
            ld_r2=ld_r2 if ld_r2 is not None else hl.null(hl.tfloat32),
            filter_exome_low_coverage_regions=filter_exome_low_coverage_regions,
            high_conf_regions=high_conf_regions
            if high_conf_regions is not None
            else hl.null(hl.tarray(hl.tstr)),
        )
    )
    return qc_mt.annotate_cols(sample_callrate=hl.agg.fraction(hl.is_defined(qc_mt.GT)))


def annotate_sex(
    mtds: Union[hl.MatrixTable, hl.vds.VariantDataset],
    is_sparse: bool = True,
    excluded_intervals: Optional[hl.Table] = None,
    included_intervals: Optional[hl.Table] = None,
    normalization_contig: str = "chr20",
    sites_ht: Optional[hl.Table] = None,
    aaf_expr: Optional[str] = None,
    gt_expr: str = "GT",
    f_stat_cutoff: float = 0.5,
    aaf_threshold: float = 0.001,
    variants_only_x_ploidy: bool = False,
    variants_only_y_ploidy: bool = False,
) -> hl.Table:
    """
    Impute sample sex based on X-chromosome heterozygosity and sex chromosome ploidy.

    Return Table with the following fields:
        - s (str): Sample
        - `normalization_contig`_mean_dp (float32): Sample's mean coverage over the specified `normalization_contig`.
        - chrX_mean_dp (float32): Sample's mean coverage over chromosome X.
        - chrY_mean_dp (float32): Sample's mean coverage over chromosome Y.
        - chrX_ploidy (float32): Sample's imputed ploidy over chromosome X.
        - chrY_ploidy (float32): Sample's imputed ploidy over chromosome Y.
        - f_stat (float64): Sample f-stat. Calculated using hl.impute_sex.
        - n_called (int64): Number of variants with a genotype call. Calculated using hl.impute_sex.
        - expected_homs (float64): Expected number of homozygotes. Calculated using hl.impute_sex.
        - observed_homs (int64): Expected number of homozygotes. Calculated using hl.impute_sex.
        - X_karyotype (str): Sample's chromosome X karyotype.
        - Y_karyotype (str): Sample's chromosome Y karyotype.
        - sex_karyotype (str): Sample's sex karyotype.

    :param mtds: Input MatrixTable or VariantDataset
    :param bool is_sparse: Whether input MatrixTable is in sparse data format
    :param excluded_intervals: Optional table of intervals to exclude from the computation.
    :param included_intervals: Optional table of intervals to use in the computation. REQUIRED for exomes.
    :param normalization_contig: Which chromosome to use to normalize sex chromosome coverage. Used in determining sex chromosome ploidies.
    :param sites_ht: Optional Table to use. If present, filters input MatrixTable to sites in this Table prior to imputing sex,
                    and pulls alternate allele frequency from this Table.
    :param aaf_expr: Optional. Name of field in input MatrixTable with alternate allele frequency.
    :param gt_expr: Name of entry field storing the genotype. Default: 'GT'
    :param f_stat_cutoff: f-stat to roughly divide 'XX' from 'XY' samples. Assumes XX samples are below cutoff and XY are above cutoff.
    :param float aaf_threshold: Minimum alternate allele frequency to be used in f-stat calculations.
    :param variants_only_x_ploidy: Whether to use depth of only variant data for the x ploidy estimation.
    :param variants_only_y_ploidy: Whether to use depth of only variant data for the y ploidy estimation.
    :return: Table of samples and their imputed sex karyotypes.
    """
    logger.info("Imputing sex chromosome ploidies...")

    is_vds = isinstance(mtds, hl.vds.VariantDataset)
    if is_vds:
        if excluded_intervals is not None:
            raise NotImplementedError(
                "The use of the parameter 'excluded_intervals' is currently not implemented for imputing sex chromosome ploidy on a VDS!"
            )
        # Begin by creating a ploidy estimate HT using the method defined by 'variants_only_x_ploidy'
        ploidy_ht = hl.vds.impute_sex_chromosome_ploidy(
            mtds,
            calling_intervals=included_intervals,
            normalization_contig=normalization_contig,
            use_variant_dataset=variants_only_x_ploidy,
        )
        ploidy_ht = ploidy_ht.rename(
            {
                "x_ploidy": "chrX_ploidy",
                "y_ploidy": "chrY_ploidy",
                "x_mean_dp": "chrX_mean_dp",
                "y_mean_dp": "chrY_mean_dp",
                "autosomal_mean_dp": f"var_data_{normalization_contig}_mean_dp"
                if variants_only_x_ploidy
                else f"{normalization_contig}_mean_dp",
            }
        )
        # If 'variants_only_y_ploidy' is different from 'variants_only_x_ploidy' then re-run the ploidy estimation using
        # the method defined by 'variants_only_y_ploidy' and re-annotate with the modified ploidy estimates.
        if variants_only_y_ploidy != variants_only_x_ploidy:
            y_ploidy_ht = hl.vds.impute_sex_chromosome_ploidy(
                mtds,
                calling_intervals=included_intervals,
                normalization_contig=normalization_contig,
                use_variant_dataset=variants_only_y_ploidy,
            )
            y_ploidy_idx = y_ploidy_ht[ploidy_ht.key]
            ploidy_ht = ploidy_ht.annotate(
                chrY_ploidy=y_ploidy_idx.y_ploidy,
                chrY_mean_dp=y_ploidy_idx.y_mean_dp,
            )

            # If the `variants_only_y_ploidy' is True modify the name of the normalization contig mean DP to indicate
            # that this is the variant dataset only mean DP (this will have already been added if
            # 'variants_only_x_ploidy' was also True).
            if variants_only_y_ploidy:
                ploidy_ht = ploidy_ht.annotate(
                    **{
                        f"var_data_{normalization_contig}_mean_dp": y_ploidy_idx.autosomal_mean_dp
                    }
                )

        mt = mtds.variant_data
    else:
        mt = mtds
        if is_sparse:
            ploidy_ht = impute_sex_ploidy(
                mt,
                excluded_intervals,
                included_intervals,
                normalization_contig,
                use_only_variants=variants_only_x_ploidy,
            )
            ploidy_ht = ploidy_ht.rename(
                {
                    "autosomal_mean_dp": f"var_data_{normalization_contig}_mean_dp"
                    if variants_only_x_ploidy
                    else f"{normalization_contig}_mean_dp",
                }
            )
            # If 'variants_only_y_ploidy' is different from 'variants_only_x_ploidy' then re-run the ploidy estimation
            # using the method defined by 'variants_only_y_ploidy' and re-annotate with the modified ploidy estimates.
            if variants_only_y_ploidy != variants_only_x_ploidy:
                y_ploidy_ht = impute_sex_ploidy(
                    mt,
                    excluded_intervals,
                    included_intervals,
                    normalization_contig,
                    use_only_variants=variants_only_y_ploidy,
                )
                y_ploidy_ht.select(
                    "chrY_ploidy",
                    "chrY_mean_dp",
                    f"{normalization_contig}_mean_dp",
                )
                # If the `variants_only_y_ploidy' is True modify the name of the normalization contig mean DP to indicate
                # that this is the variant dataset only mean DP (this will have already been added if
                # 'variants_only_x_ploidy' was also True).
                if variants_only_y_ploidy:
                    ploidy_ht = ploidy_ht.rename(
                        {
                            f"{normalization_contig}_mean_dp": f"var_data_{normalization_contig}_mean_dp"
                        }
                    )
                # Re-annotate the ploidy HT with modified Y ploidy annotations
                ploidy_ht = ploidy_ht.annotate(**y_ploidy_ht[ploidy_ht.key])

        else:
            raise NotImplementedError(
                "Imputing sex ploidy does not exist yet for dense data."
            )

    x_contigs = get_reference_genome(mt.locus).x_contigs
    logger.info("Filtering mt to biallelic SNPs in X contigs: %s", x_contigs)
    if "was_split" in list(mt.row):
        mt = mt.filter_rows((~mt.was_split) & hl.is_snp(mt.alleles[0], mt.alleles[1]))
    else:
        mt = mt.filter_rows(
            (hl.len(mt.alleles) == 2) & hl.is_snp(mt.alleles[0], mt.alleles[1])
        )

    build = get_reference_genome(mt.locus).name
    mt = hl.filter_intervals(
        mt,
        [
            hl.parse_locus_interval(contig, reference_genome=build)
            for contig in x_contigs
        ],
        keep=True,
    )

    if sites_ht is not None:
        if aaf_expr == None:
            logger.warning(
                "sites_ht was provided, but aaf_expr is missing. Assuming name of field with alternate allele frequency is 'AF'."
            )
            aaf_expr = "AF"
        logger.info("Filtering to provided sites")
        mt = mt.annotate_rows(**sites_ht[mt.row_key])
        mt = mt.filter_rows(hl.is_defined(mt[aaf_expr]))

    logger.info("Calculating inbreeding coefficient on chrX")
    sex_ht = hl.impute_sex(
        mt[gt_expr],
        aaf_threshold=aaf_threshold,
        male_threshold=f_stat_cutoff,
        female_threshold=f_stat_cutoff,
        aaf=aaf_expr,
    )

    logger.info("Annotating sex ht with sex chromosome ploidies")
    sex_ht = sex_ht.annotate(**ploidy_ht[sex_ht.key])

    logger.info("Inferring sex karyotypes")
    x_ploidy_cutoffs, y_ploidy_cutoffs = get_ploidy_cutoffs(sex_ht, f_stat_cutoff)
    sex_ht = sex_ht.annotate_globals(
        x_ploidy_cutoffs=hl.struct(
            upper_cutoff_X=x_ploidy_cutoffs[0],
            lower_cutoff_XX=x_ploidy_cutoffs[1][0],
            upper_cutoff_XX=x_ploidy_cutoffs[1][1],
            lower_cutoff_XXX=x_ploidy_cutoffs[2],
        ),
        y_ploidy_cutoffs=hl.struct(
            lower_cutoff_Y=y_ploidy_cutoffs[0][0],
            upper_cutoff_Y=y_ploidy_cutoffs[0][1],
            lower_cutoff_YY=y_ploidy_cutoffs[1],
        ),
        f_stat_cutoff=f_stat_cutoff,
        variants_only_x_ploidy=variants_only_x_ploidy,
        variants_only_y_ploidy=variants_only_y_ploidy,
    )
    return sex_ht.annotate(
        **get_sex_expr(
            sex_ht.chrX_ploidy, sex_ht.chrY_ploidy, x_ploidy_cutoffs, y_ploidy_cutoffs
        )
    )
