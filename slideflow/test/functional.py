import logging
import os
from os.path import exists, join
from typing import TYPE_CHECKING, List, Tuple, Union

import slideflow as sf
from PIL import Image
from slideflow.stats import SlideMap
from slideflow.test.utils import (handle_errors, test_multithread_throughput,
                                  test_throughput)
from slideflow.util import colors as col
from tqdm import tqdm

if TYPE_CHECKING:
    import multiprocessing


@handle_errors
def activations_tester(
    project: sf.Project,
    verbosity: int,
    passed: "multiprocessing.managers.ValueProxy",
    model: str,
    **kwargs
) -> None:
    """Tests generation of intermediate layer activations.

    Function must happen in an isolated process to free GPU memory when done.
    """
    logging.getLogger("slideflow").setLevel(verbosity)

    # Test activations generation.
    dataset = project.dataset(71, 1208)
    test_slide = dataset.slides()[0]

    df = project.generate_features(
        model=model,
        outcomes='category1',
        **kwargs
    )
    act_by_cat = df.activations_by_category(0).values()
    assert df.num_features == 1280  # mobilenet_v2
    assert df.num_logits == 2
    assert len(df.activations) == len(dataset.tfrecords())
    assert len(df.locations) == len(df.activations) == len(df.logits)
    assert all([
        len(df.activations[s]) == len(df.logits[s]) == len(df.locations[s])
        for s in df.activations
    ])
    assert len(df.activations_by_category(0)) == 2
    assert (sum([len(a) for a in act_by_cat])
            == sum([len(df.activations[s]) for s in df.slides]))
    lm = df.logits_mean()
    l_perc = df.logits_percent()
    l_pred = df.logits_predict()
    assert len(lm) == len(df.activations)
    assert len(lm[test_slide]) == df.num_logits
    assert len(l_perc) == len(df.activations)
    assert len(l_perc[test_slide]) == df.num_logits
    assert len(l_pred) == len(df.activations)

    umap = SlideMap.from_features(df)
    if not exists(join(project.root, 'stats')):
        os.makedirs(join(project.root, 'stats'))
    umap.save(join(project.root, 'stats', '2d_umap.png'))
    tile_stats, pt_stats, cat_stats = df.stats()
    top_features_by_tile = sorted(
        range(df.num_features),
        key=lambda f: tile_stats[f]['p']
    )
    for feature in top_features_by_tile[:5]:
        umap.save_3d_plot(
            join(project.root, 'stats', f'3d_feature{feature}.png'),
            feature=feature
        )
    df.box_plots(
        top_features_by_tile[:5],
        join(project.root, 'box_plots')
    )

    # Test mosaic.
    mosaic = project.generate_mosaic(df)
    mosaic.save(join(project.root, "mosaic_test.png"), resolution='low')


@handle_errors
def clam_feature_generator_tester(
    project: sf.Project,
    verbosity: int,
    passed: "multiprocessing.managers.ValueProxy",
    model: str,
) -> None:
    """Tests feature generation for CLAM (and related) models.

    Function must happen in an isolated process to free GPU memory when done.
    """
    logging.getLogger("slideflow").setLevel(verbosity)
    outdir = join(project.root, 'clam')
    project.generate_features_for_clam(
        model,
        outdir=outdir,
        force_regenerate=True
    )


@handle_errors
def evaluation_tester(project, verbosity, passed, **kwargs) -> None:
    """Tests model evaluation.

    Function must happen in an isolated process to free GPU memory when done.
    """
    logging.getLogger("slideflow").setLevel(verbosity)
    project.evaluate(**kwargs)


@handle_errors
def prediction_tester(project, verbosity, passed, **kwargs) -> None:
    """Tests model predictions.

    Function must happen in an isolated process to free GPU memory when done.
    """
    logging.getLogger("slideflow").setLevel(verbosity)
    project.predict(**kwargs)


@handle_errors
def reader_tester(project, verbosity, passed) -> None:
    """Tests TFRecord reading between backends and ensures identical results.

    Function must happen in an isolated process to free GPU memory when done.
    """
    dataset = project.dataset(71, 1208)
    tfrecords = dataset.tfrecords()
    batch_size = 128
    assert len(tfrecords)

    # Torch backend
    torch_results = []
    torch_dts = dataset.torch(
        labels=None,
        batch_size=batch_size,
        infinite=False,
        augment=False,
        standardize=False,
        num_workers=6,
        pin_memory=False
    )
    if verbosity < logging.WARNING:
        torch_dts = tqdm(
            torch_dts,
            leave=False,
            ncols=80,
            unit_scale=batch_size,
            total=dataset.num_tiles // batch_size
        )
    for images, labels in torch_dts:
        torch_results += [
            hash(str(img.numpy().transpose(1, 2, 0)))  # CWH -> WHC
            for img in images
        ]
    if verbosity < logging.WARNING:
        torch_dts.close()  # type: ignore
    torch_results = sorted(torch_results)

    # Tensorflow backend
    tf_results = []
    tf_dts = dataset.tensorflow(
        labels=None,
        batch_size=batch_size,
        infinite=False,
        augment=False,
        standardize=False
    )
    if verbosity < logging.WARNING:
        tf_dts = tqdm(
            tf_dts,
            leave=False,
            ncols=80,
            unit_scale=batch_size,
            total=dataset.num_tiles // batch_size
        )
    for images, labels in tf_dts:
        tf_results += [hash(str(img.numpy())) for img in images]
    if verbosity < logging.WARNING:
        tf_dts.close()
    tf_results = sorted(tf_results)

    assert len(torch_results) == len(tf_results) == dataset.num_tiles
    assert torch_results == tf_results


@handle_errors
def single_thread_normalizer_tester(
    project: sf.Project,
    verbosity: int,
    passed: "multiprocessing.managers.ValueProxy",
    methods: Union[List, Tuple],
) -> None:
    """Tests all normalization strategies and throughput.

    Function must happen in an isolated process to free GPU memory when done.
    """
    logging.getLogger("slideflow").setLevel(verbosity)
    if not len(methods):
        methods = sf.norm.StainNormalizer.normalizers  # type: ignore
    dataset = project.dataset(71, 1208)
    v = '(vectorized)'

    dts_kw = {'standardize': False, 'infinite': True}
    if sf.backend() == 'tensorflow':
        dts = dataset.tensorflow(None, None, **dts_kw)
        raw_img = next(iter(dts))[0].numpy()
    elif sf.backend() == 'torch':
        dts = dataset.torch(None, None, **dts_kw)
        raw_img = next(iter(dts))[0].permute(1, 2, 0).numpy()
    Image.fromarray(raw_img).save(join(project.root, 'raw_img.png'))
    for method in methods:
        gen_norm = sf.norm.autoselect(method, prefer_vectorized=False)
        vec_norm = sf.norm.autoselect(method, prefer_vectorized=True)
        st_msg = col.yellow('SINGLE-thread')
        print(f"'\r\033[kTesting {method} [{st_msg}]...", end="")

        # Save example image
        img = Image.fromarray(gen_norm.rgb_to_rgb(raw_img))
        img.save(join(project.root, f'{method}.png'))

        gen_tpt = test_throughput(dts, gen_norm)
        dur = col.blue(f"[{gen_tpt:.1f} img/s]")
        print(f"'\r\033[kTesting {method} [{st_msg}]... DONE " + dur)
        if type(vec_norm) != type(gen_norm):
            print(f"'\r\033[kTesting {method} {v} [{st_msg}]...", end="")

            # Save example image
            img = Image.fromarray(vec_norm.rgb_to_rgb(raw_img))
            img.save(join(project.root, f'{method}_vectorized.png'))

            vec_tpt = test_throughput(dts, vec_norm)
            dur = col.blue(f"[{vec_tpt:.1f} img/s]")
            print(f"'\r\033[kTesting {method} {v} [{st_msg}]... DONE {dur}")


@handle_errors
def multi_thread_normalizer_tester(
    project: sf.Project,
    verbosity: int,
    passed: "multiprocessing.managers.ValueProxy",
    methods: Union[List, Tuple],
) -> None:
    """Tests all normalization strategies and throughput.

    Function must happen in an isolated process to free GPU memory when done.
    """
    logging.getLogger("slideflow").setLevel(verbosity)
    if not len(methods):
        methods = sf.norm.StainNormalizer.normalizers  # type: ignore
    dataset = project.dataset(71, 1208)
    v = '(vectorized)'

    for method in methods:
        gen_norm = sf.norm.autoselect(method, prefer_vectorized=False)
        vec_norm = sf.norm.autoselect(method, prefer_vectorized=True)
        mt_msg = col.purple('MULTI-thread')
        print(f"'\r\033[kTesting {method} [{mt_msg}]...", end="")
        gen_tpt = test_multithread_throughput(dataset, gen_norm)
        dur = col.blue(f"[{gen_tpt:.1f} img/s]")
        print(f"'\r\033[kTesting {method} [{mt_msg}]... DONE " + dur)
        if type(vec_norm) != type(gen_norm):
            print(f"'\r\033[kTesting {method} {v} [{mt_msg}]...", end="")
            vec_tpt = test_multithread_throughput(dataset, vec_norm)
            dur = col.blue(f"[{vec_tpt:.1f} img/s]")
            print(f"'\r\033[kTesting {method} {v} [{mt_msg}]... DONE " + dur)


@handle_errors
def wsi_prediction_tester(
    project: sf.Project,
    verbosity: int,
    passed: "multiprocessing.managers.ValueProxy",
    model: str,
) -> None:
    """Tests predictions of whole-slide images.

    Function must happen in an isolated process to free GPU memory when done.
    """
    logging.getLogger("slideflow").setLevel(verbosity)
    dataset = project.dataset()
    slide_paths = dataset.slide_paths(source='TEST')
    patient_name = sf.util.path_to_name(slide_paths[0])
    project.predict_wsi(
        model,
        join(project.root, 'wsi'),
        filters={'patient': [patient_name]}
    )
