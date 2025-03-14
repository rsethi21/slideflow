
import logging
import os
import time
import traceback
import unittest
from os.path import exists, join
from typing import Optional

import slideflow as sf
import slideflow.test.functional
from slideflow import errors
from slideflow.test import dataset_test, slide_test, stats_test
from slideflow.test.utils import (TaskWrapper, TestConfig,
                                  _assert_valid_results, process_isolate)
from slideflow.util import colors as col
from slideflow.util import log


class TestSuite:
    """Supervises functional testing of the Slideflow pipeline."""
    def __init__(
        self,
        root: str,
        slides: Optional[str] = None,
        buffer: Optional[str] = None,
        verbosity: int = logging.WARNING,
        reset: bool = False
    ) -> None:
        """Prepare for functional and unit testing testing. Functional tests
        require example slides.

        Args:
            root (str): Root directory of test project.
            slides (str, optional): Path to folder containing test slides.
            buffer (str, optional): Buffer slides to this location for faster
                testing. Defaults to None.
            verbosity (int, optional): Logging level. Defaults to
                logging.WARNING.
            reset (bool, optional): Reset the test project folder before
                starting. Defaults to False.

        Raises:
            errors.BackendError: If the environmental variable SF_BACKEND
                is not either "tensorflow" or "torch".
        """

        if slides is None:
            print(col.yellow("Path to slides not provided, unable to perform"
                             " functional tests."))
            self.project = None
            return
        else:
            detected_slides = [
                sf.util.path_to_name(f)
                for f in os.listdir(slides)
                if sf.util.path_to_ext(f).lower() in sf.util.SUPPORTED_FORMATS
            ][:10]
            if not len(detected_slides):
                print(col.yellow(f"No slides found at {slides}; "
                                 "unable to perform functional tests."))
                self.project = None
                return

        # --- Set up project --------------------------------------------------
        # Set logging level
        logging.getLogger("slideflow").setLevel(verbosity)
        # Set the tensorflow logger
        if logging.getLogger('slideflow').level == logging.DEBUG:
            logging.getLogger('tensorflow').setLevel(logging.DEBUG)
            os.environ['TF_CPP_MIN_LOG_LEVEL'] = '0'
        else:
            logging.getLogger('tensorflow').setLevel(logging.ERROR)
            os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
        self.verbosity = verbosity
        TaskWrapper.VERBOSITY = verbosity

        # Configure testing environment
        self.test_root = root
        self.project_root = join(root, 'project')
        self.slides_root = slides
        print(f'Setting up test project at {col.green(root)}')
        print(f'Testing using slides from {col.green(slides)}')
        self.config = TestConfig(root, slides=slides)
        self.project = self.config.create_project(
            self.project_root,
            overwrite=reset
        )

        # Check if GPU available
        if sf.backend() == 'tensorflow':
            import tensorflow as tf
            if not tf.config.list_physical_devices('GPU'):
                log.error("GPU unavailable - tests may fail.")
        elif sf.backend() == 'torch':
            import torch
            if not torch.cuda.is_available():
                log.error("GPU unavailable - tests may fail.")
        else:
            raise errors.BackendError(
                f"Unknown backend {sf.backend()} "
                "Valid backends: 'tensorflow' or 'torch'"
            )

        # Configure datasets (input)
        self.buffer = buffer

        # Rebuild tfrecord indices
        self.project.dataset(71, 1208).build_index(True)

    def _get_model(self, name: str, epoch: int = 1) -> str:
        assert self.project is not None
        prev_run_dirs = [
            x for x in os.listdir(self.project.models_dir)
            if os.path.isdir(join(self.project.models_dir, x))
        ]
        for run in sorted(prev_run_dirs, reverse=True):
            if run[6:] == name:
                return join(
                    self.project.models_dir,
                    run,
                    f'{name}_epoch{epoch}'
                )
        raise OSError(f"Unable to find trained model {name}")

    def setup_hp(
        self,
        model_type: str,
        sweep: bool = False,
        normalizer: Optional[str] = None,
        uq: bool = False
    ) -> sf.ModelParams:
        """Set up hyperparameters.

        Args:
            model_type (str): Type of model, ('categorical', 'linear, 'cph').
            sweep (bool, optional): Set up HP sweep. Defaults to False.
            normalizer (str, optional): Normalizer strategy. Defaults to None.
            uq (bool, optional): Uncertainty quantification. Defaults to False.

        Returns:
            sf.ModelParams: Hyperparameter object.
        """

        assert self.project is not None
        if model_type == 'categorical':
            loss = ('sparse_categorical_crossentropy'
                    if sf.backend() == 'tensorflow'
                    else 'CrossEntropy')
        elif model_type == 'linear':
            loss = ('mean_squared_error'
                    if sf.backend() == 'tensorflow'
                    else 'MSE')
        elif model_type == 'cph':
            loss = ('negative_log_likelihood'
                    if sf.backend() == 'tensorflow'
                    else 'NLL')

        # Create batch train file
        if sweep:
            self.project.create_hp_sweep(
                tile_px=71,
                tile_um=1208,
                epochs=[1, 3],
                toplayer_epochs=[0],
                model=["mobilenet_v2"],
                loss=[loss],
                learning_rate=[0.001],
                batch_size=[16],
                hidden_layers=[0, 1],
                optimizer=["Adam"],
                early_stop=[False],
                early_stop_patience=[15],
                early_stop_method='loss',
                hidden_layer_width=500,
                trainable_layers=0,
                dropout=0.1,
                training_balance=["category"],
                validation_balance=["none"],
                augment=[True],
                normalizer=normalizer,
                label='TEST',
                uq=uq,
                filename='sweep.json'
            )

        # Create single hyperparameter combination
        hp = sf.model.ModelParams(
            tile_px=71,
            tile_um=1208,
            epochs=1,
            toplayer_epochs=0,
            model="mobilenet_v2",
            pooling='max',
            loss=loss,
            learning_rate=0.001,
            batch_size=16,
            hidden_layers=1,
            optimizer='Adam',
            early_stop=False,
            dropout=0.1,
            early_stop_patience=0,
            training_balance='patient',
            validation_balance='none',
            uq=uq,
            augment=True
        )
        return hp

    def test_extraction(self, enable_downsample: bool = True, **kwargs) -> None:
        """Test tile extraction.

        Args:
            enable_downsample (bool, optional): Enable using intermediate
                downsample layers in slides. Defaults to True.
        """
        assert self.project is not None
        with TaskWrapper("Testing slide extraction...") as test:
            try:
                self.project.extract_tiles(
                    tile_px=71,
                    tile_um=1208,
                    buffer=self.buffer,
                    source=['TEST'],
                    roi_method='ignore',
                    skip_extracted=False,
                    img_format='png',
                    enable_downsample=enable_downsample,
                    **kwargs
                )
                self.project.extract_tiles(
                    tile_px=71,
                    tile_um="2.5x",
                    buffer=self.buffer,
                    source=['TEST'],
                    roi_method='ignore',
                    img_format='png',
                    enable_downsample=enable_downsample,
                    dry_run=True,
                    **kwargs
                )
            except Exception as e:
                log.error(traceback.format_exc())
                test.fail()

    def test_normalizers(
        self,
        *args,
        single: bool = True,
        multi: bool = True,
    ) -> None:
        """Test normalizer strategy and throughput, saving example image
        for each.

        Args:
            single (bool, optional): Perform single-thread tests.
                Defaults to True.
            multi (bool, optional): Perform multi-thread tests.
                Defaults to True.
        """
        assert self.project is not None
        if single:
            with TaskWrapper("Testing normalization single-thread throughput...") as test:
                passed = process_isolate(
                    sf.test.functional.single_thread_normalizer_tester,
                    project=self.project,
                    methods=args,
                )
                if not passed:
                    test.fail()
        if multi:
            with TaskWrapper("Testing normalization multi-thread throughput...") as test:
                passed = process_isolate(
                    sf.test.functional.multi_thread_normalizer_tester,
                    project=self.project,
                    methods=args,
                )
                if not passed:
                    test.fail()

    def test_readers(self) -> None:
        """Test TFRecord reading between backends (Tensorflow/PyTorch), ensuring
        that both yield identical results.
        """
        assert self.project is not None
        with TaskWrapper("Testing torch and tensorflow readers...") as test:
            try:
                import tensorflow as tf  # noqa F401
                import torch  # noqa F401
            except ImportError:
                log.warning(
                    "Can't import tensorflow and pytorch, skipping TFRecord test"
                )
                test.skip()
                return
            passed = process_isolate(
                sf.test.functional.reader_tester,
                project=self.project
            )
            if not passed:
                test.fail()

    def train_perf(self, **train_kwargs) -> None:
        """Test model training across multiple epochs."""

        assert self.project is not None
        msg = "Training single categorical outcome from HP sweep..."
        with TaskWrapper(msg) as test:
            try:
                self.setup_hp(
                    'categorical',
                    sweep=True,
                    normalizer='reinhard_fast',
                    uq=False
                )
                results = self.project.train(
                    exp_label='manual_hp',
                    outcomes='category1',
                    val_k=1,
                    validate_on_batch=10,
                    save_predictions=True,
                    steps_per_epoch_override=20,
                    params='sweep.json',
                    pretrain=None,
                    **train_kwargs
                )
                _assert_valid_results(results)
            except Exception as e:
                log.error(traceback.format_exc())
                test.fail()

    def test_training(
        self,
        categorical: bool = True,
        uq: bool = True,
        multi_categorical: bool = True,
        linear: bool = True,
        multi_linear: bool = True,
        multi_input: bool = True,
        cph: bool = True,
        multi_cph: bool = True,
        **train_kwargs
    ) -> None:
        """Test model training using a variety of strategies.

        Models are trained for one epoch for only 20 steps.

        Args:
            categorical (bool, optional): Test training a single outcome,
                multi-class categorical model. Defaults to True.
            uq (bool, optional): Test training with UQ. Defaults to True.
            multi_categorical (bool, optional): Test training a multi-outcome,
                multi-class categorical model. Defaults to True.
            linear (bool, optional): Test training a continuous outcome.
                Defaults to True.
            multi_linear (bool, optional): Test training with multiple
                continuous outcomes. Defaults to True.
            multi_input (bool, optional): Test training with slide-level input
                in addition to image input. Defaults to True.
            cph (bool, optional): Test training a Cox-Proportional Hazards
                model. Defaults to True.
            multi_cph (bool, optional): Test training a CPH model with
                additional slide-level input. Defaults to True.
        """
        assert self.project is not None
        # Disable checkpoints for tensorflow backend, to save disk space
        if (sf.backend() == 'tensorflow'
           and 'save_checkpoints' not in train_kwargs):
            train_kwargs['save_checkpoints'] = False

        if categorical:
            # Test categorical outcome
            self.train_perf(**train_kwargs)

        if uq:
            # Test categorical outcome with UQ
            msg = "Training single categorical outcome with UQ..."
            with TaskWrapper(msg) as test:
                try:
                    hp = self.setup_hp('categorical', sweep=False, uq=True)
                    results = self.project.train(
                        exp_label='UQ',
                        outcomes='category1',
                        val_k=1,
                        params=hp,
                        validate_on_batch=10,
                        steps_per_epoch_override=20,
                        save_predictions=True,
                        pretrain=None,
                        **train_kwargs
                    )
                    _assert_valid_results(results)
                except Exception as e:
                    log.error(traceback.format_exc())
                    test.fail()

        if multi_categorical:
            # Test multiple sequential categorical outcome models
            with TaskWrapper("Training to multiple outcomes...") as test:
                try:
                    results = self.project.train(
                        outcomes=['category1', 'category2'],
                        val_k=1,
                        params=self.setup_hp('categorical'),
                        validate_on_batch=10,
                        steps_per_epoch_override=20,
                        save_predictions=True,
                        pretrain=None,
                        **train_kwargs
                    )
                    _assert_valid_results(results)
                except Exception as e:
                    log.error(traceback.format_exc())
                    test.fail()

        if linear:
            # Test single linear outcome
            with TaskWrapper("Training with single linear outcome...") as test:
                try:
                    results = self.project.train(
                        outcomes=['linear1'],
                        val_k=1,
                        params=self.setup_hp('linear'),
                        validate_on_batch=10,
                        steps_per_epoch_override=20,
                        save_predictions=True,
                        pretrain=None,
                        **train_kwargs
                    )
                    _assert_valid_results(results)
                except Exception as e:
                    log.error(traceback.format_exc())
                    test.fail()

        if multi_linear:
            # Test multiple linear outcome
            with TaskWrapper("Training multiple linear outcomes...") as test:
                try:
                    results = self.project.train(
                        outcomes=['linear1', 'linear2'],
                        val_k=1,
                        params=self.setup_hp('linear'),
                        validate_on_batch=10,
                        steps_per_epoch_override=20,
                        save_predictions=True,
                        pretrain=None,
                        **train_kwargs
                    )
                    _assert_valid_results(results)
                except Exception as e:
                    log.error(traceback.format_exc())
                    test.fail()

        if multi_input:
            msg = 'Training with multiple inputs (image + slide feature)...'
            with TaskWrapper(msg) as test:
                try:
                    results = self.project.train(
                        exp_label='multi_input',
                        outcomes='category1',
                        input_header='category2',
                        params=self.setup_hp('categorical'),
                        val_k=1,
                        validate_on_batch=10,
                        steps_per_epoch_override=20,
                        save_predictions=True,
                        pretrain=None,
                        **train_kwargs
                    )
                    _assert_valid_results(results)
                except Exception as e:
                    log.error(traceback.format_exc())
                    test.fail()

        if cph:
            with TaskWrapper("Training a CPH model...") as test:
                if sf.backend() == 'tensorflow':
                    try:
                        results = self.project.train(
                            exp_label='cph',
                            outcomes='time',
                            input_header='event',
                            params=self.setup_hp('cph'),
                            val_k=1,
                            validate_on_batch=10,
                            steps_per_epoch_override=20,
                            save_predictions=True,
                            pretrain=None,
                            **train_kwargs
                        )
                        _assert_valid_results(results)
                    except Exception as e:
                        log.error(traceback.format_exc())
                        test.fail()
                else:
                    test.skip()

        if multi_cph:
            with TaskWrapper("Training a multi-input CPH model...") as test:
                if sf.backend() == 'tensorflow':
                    try:
                        results = self.project.train(
                            exp_label='multi_cph',
                            outcomes='time',
                            input_header=['event', 'category1'],
                            params=self.setup_hp('cph'),
                            val_k=1,
                            validate_on_batch=10,
                            steps_per_epoch_override=20,
                            save_predictions=True,
                            pretrain=None,
                            **train_kwargs
                        )
                        _assert_valid_results(results)
                    except Exception as e:
                        log.error(traceback.format_exc())
                        test.fail()
                else:
                    test.skip()
        else:
            print("Skipping CPH model testing [current backend is Pytorch]")

    def test_prediction(self, **predict_kwargs) -> None:
        """Test prediction generation using a previously trained model."""

        assert self.project is not None
        model = self._get_model('category1-manual_hp-TEST-HPSweep0-kfold1')

        with TaskWrapper("Testing categorical model predictions...") as test:
            passed = process_isolate(
                sf.test.functional.prediction_tester,
                project=self.project,
                model=model,
                **predict_kwargs
            )
            if not passed:
                test.fail()

    def test_evaluation(self, **eval_kwargs) -> None:
        """Test evaluation of previously trained models."""

        assert self.project is not None
        multi_cat_model = self._get_model('category1-category2-HP0-kfold1')
        multi_lin_model = self._get_model('linear1-linear2-HP0-kfold1')
        multi_inp_model = self._get_model('category1-multi_input-HP0-kfold1')
        f_model = self._get_model('category1-manual_hp-TEST-HPSweep0-kfold1')

        # Performs evaluation in isolated thread to avoid OOM errors
        # with sequential model loading/testing
        with TaskWrapper("Testing categorical model evaluation...") as test:
            passed = process_isolate(
                sf.test.functional.evaluation_tester,
                project=self.project,
                model=f_model,
                outcomes='category1',
                histogram=True,
                save_predictions=True,
                **eval_kwargs
            )
            if not passed:
                test.fail()

        with TaskWrapper("Testing categorical UQ model evaluation...") as test:
            uq_model = self._get_model('category1-UQ-HP0-kfold1')
            passed = process_isolate(
                sf.test.functional.evaluation_tester,
                project=self.project,
                model=uq_model,
                outcomes='category1',
                histogram=True,
                save_predictions=True,
                **eval_kwargs
            )
            if not passed:
                test.fail()

        with TaskWrapper("Testing multi-categorical model evaluation...") as test:
            passed = process_isolate(
                sf.test.functional.evaluation_tester,
                project=self.project,
                model=multi_cat_model,
                outcomes=['category1', 'category2'],
                histogram=True,
                save_predictions=True,
                **eval_kwargs
            )
            if not passed:
                test.fail()

        with TaskWrapper("Testing multi-linear model evaluation...") as test:
            passed = process_isolate(
                sf.test.functional.evaluation_tester,
                project=self.project,
                model=multi_lin_model,
                outcomes=['linear1', 'linear2'],
                histogram=True,
                save_predictions=True,
                **eval_kwargs
            )
            if not passed:
                test.fail()

        with TaskWrapper("Testing multi-input model evaluation...") as test:
            passed = process_isolate(
                sf.test.functional.evaluation_tester,
                project=self.project,
                model=multi_inp_model,
                outcomes='category1',
                input_header='category2',
                **eval_kwargs
            )
            if not passed:
                test.fail()

        with TaskWrapper("Testing CPH model evaluation...") as test:
            if sf.backend() == 'tensorflow':
                cph_model = self._get_model('time-cph-HP0-kfold1')
                passed = process_isolate(
                    sf.test.functional.evaluation_tester,
                    project=self.project,
                    model=cph_model,
                    outcomes='time',
                    input_header='event',
                    **eval_kwargs
                )
                if not passed:
                    test.fail()
            else:
                test.skip()

    def test_heatmap(self, slide: str = 'auto', **heatmap_kwargs) -> None:
        """Test heatmap generation using a previously trained model."""

        assert self.project is not None
        model = self._get_model('category1-manual_hp-TEST-HPSweep0-kfold1')
        assert exists(model), "Model has not yet been trained."

        with TaskWrapper("Testing heatmap generation...") as test:
            try:
                if slide.lower() == 'auto':
                    dataset = self.project.dataset()
                    slide_paths = dataset.slide_paths(source='TEST')
                    patient_name = sf.util.path_to_name(slide_paths[0])
                self.project.generate_heatmaps(
                    model,
                    filters={'patient': [patient_name]},
                    roi_method='ignore',
                    **heatmap_kwargs
                )
            except Exception as e:
                log.error(traceback.format_exc())
                test.fail()

    def test_activations_and_mosaic(self, **act_kwargs) -> None:
        """Test calculation of final-layer activations & creation
        of a mosaic map.
        """
        assert self.project is not None
        model = self._get_model('category1-manual_hp-TEST-HPSweep0-kfold1')
        assert exists(model), "Model has not yet been trained."
        with TaskWrapper("Testing activations and mosaic...") as test:
            passed = process_isolate(
                sf.test.functional.activations_tester,
                project=self.project,
                model=model,
                **act_kwargs
            )
            if not passed:
                test.fail()

    def test_predict_wsi(self) -> None:
        """Test predictions for whole-slide images."""

        assert self.project is not None
        model = self._get_model('category1-manual_hp-TEST-HPSweep0-kfold1')
        assert exists(model), "Model has not yet been trained."
        with TaskWrapper("Testing WSI prediction...") as test:
            passed = process_isolate(
                sf.test.functional.wsi_prediction_tester,
                project=self.project,
                model=model
            )
            if not passed:
                test.fail()

    def test_clam(self) -> None:
        """Test the CLAM submodule."""

        assert self.project is not None
        model = self._get_model('category1-manual_hp-TEST-HPSweep0-kfold1')
        assert exists(model), "Model has not yet been trained."

        try:
            skip_test = False
            import torch  # noqa F401
        except ImportError:
            log.warning("Unable to import pytorch, skipping CLAM test")
            skip_test = True

        with TaskWrapper("Testing CLAM feature export...") as test:
            if skip_test:
                test.skip()
            else:
                passed = process_isolate(
                    sf.test.functional.clam_feature_generator_tester,
                    project=self.project,
                    model=model
                )
                if not passed:
                    test.fail()

        with TaskWrapper("Testing CLAM training...") as test:
            if skip_test:
                test.skip()
            else:
                try:
                    dataset = self.project.dataset(71, 1208)
                    self.project.train_clam(
                        'TEST_CLAM',
                        join(self.project.root, 'clam'),
                        'category1',
                        dataset
                    )
                except Exception as e:
                    log.error(traceback.format_exc())
                    test.fail()

    def test(
        self,
        extract: bool = True,
        reader: bool = True,
        train: bool = True,
        normalizer: bool = True,
        evaluate: bool = True,
        predict: bool = True,
        heatmap: bool = True,
        activations: bool = True,
        predict_wsi: bool = True,
        clam: bool = True
    ) -> None:
        """Perform and report results of all available testing."""

        start = time.time()
        self.unittests()
        if self.project is None:
            print(col.yellow("Slides not provided; unable to perform "
                             "functional or WSI testing."))
        else:
            if extract:
                self.test_extraction()
            if reader:
                self.test_readers()
            if train:
                self.test_training()
            if normalizer:
                self.test_normalizers()
            if evaluate:
                self.test_evaluation()
            if predict:
                self.test_prediction()
            if heatmap:
                self.test_heatmap()
            if activations:
                self.test_activations_and_mosaic()
            if predict_wsi:
                self.test_predict_wsi()
            if clam:
                self.test_clam()
        end = time.time()
        m, s = divmod(end-start, 60)
        print(f'Tests complete. Time: {int(m)} min, {s:.2f} sec')

    def unittests(self) -> None:
        """Run unit tests."""

        print("Running unit tests...")
        runner = unittest.TextTestRunner()
        all_tests = [
            unittest.TestLoader().loadTestsFromModule(module)
            for module in (dataset_test, stats_test)
        ]
        suite = unittest.TestSuite(all_tests)

        # Add WSI tests if slides are provided
        if self.project is not None:
            test_slide = self.project.dataset().slide_paths()[0]
            test_loader = unittest.TestLoader()
            test_names = test_loader.getTestCaseNames(slide_test.TestSlide)
            for test_name in test_names:
                suite.addTest(slide_test.TestSlide(test_name, test_slide))

        runner.run(suite)
