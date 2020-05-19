# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import glob
import logging
import numpy as np
import os
import tempfile
from collections import OrderedDict
import torch
from fvcore.common.file_io import PathManager
from PIL import Image

from detectron2.data import MetadataCatalog
from detectron2.utils import comm

from .evaluator import DatasetEvaluator


class CityscapesEvaluator(DatasetEvaluator):
    """
    Base class for evaluation using cityscapes API.
    """

    def __init__(self, cfg, dataset_name):
        """
        Args:
            dataset_name (str): the name of the dataset.
                It must have the following metadata associated with it:
                "thing_classes", "gt_dir".
        """
        self.dataset_type = cfg.DATASETS.ROAD_SCENE
        self._metadata = MetadataCatalog.get(dataset_name)
        self._cpu_device = torch.device("cpu")
        self._logger = logging.getLogger(__name__)
        self.inf_start = cfg.DATASETS.INF_START
        self.skip_interv = cfg.DATASETS.SKIP_EVAL

    def reset(self):
        self._working_dir = tempfile.TemporaryDirectory(prefix="cityscapes_eval_")
        self._temp_dir = self._working_dir.name
        # All workers will write to the same results directory
        # TODO this does not work in distributed training
        self._temp_dir = comm.all_gather(self._temp_dir)[0]
        if self._temp_dir != self._working_dir.name:
            self._working_dir.cleanup()
        self._logger.info(
            "Writing cityscapes results to temporary directory {} ...".format(self._temp_dir)
        )


class CityscapesInstanceEvaluator(CityscapesEvaluator):
    """
    Evaluate instance segmentation results using cityscapes API.

    Note:
        * It does not work in multi-machine distributed training.
        * It contains a synchronization, therefore has to be used on all ranks.
        * Only the main process runs evaluation.
    """

    def process(self, inputs, outputs):
        from cityscapesscripts.helpers.labels import name2label

        for input, output in zip(inputs, outputs):
            file_name = input["file_name"]
            basename = os.path.splitext(os.path.basename(file_name))[0]
            pred_txt = os.path.join(self._temp_dir, basename + "_pred.txt")

            output = output["instances"].to(self._cpu_device)
            num_instances = len(output)
            with open(pred_txt, "w") as fout:
                for i in range(num_instances):
                    pred_class = output.pred_classes[i]
                    classes = self._metadata.thing_classes[pred_class]
                    class_id = name2label[classes].id
                    score = output.scores[i]
                    mask = output.pred_masks[i].numpy().astype("uint8")
                    png_filename = os.path.join(
                        self._temp_dir, basename + "_{}_{}.png".format(i, classes)
                    )

                    Image.fromarray(mask * 255).save(png_filename)
                    fout.write("{} {} {}\n".format(os.path.basename(png_filename), class_id, score))

    def evaluate(self):
        """
        Returns:
            dict: has a key "segm", whose value is a dict of "AP" and "AP50".
        """
        comm.synchronize()
        if comm.get_rank() > 0:
            return
        import cityscapesscripts.evaluation.evalInstanceLevelSemanticLabeling as cityscapes_eval

        self._logger.info("Evaluating results under {} ...".format(self._temp_dir))

        # set some global states in cityscapes evaluation API, before evaluating
        cityscapes_eval.args.predictionPath = os.path.abspath(self._temp_dir)
        cityscapes_eval.args.predictionWalk = None
        cityscapes_eval.args.JSONOutput = False
        cityscapes_eval.args.colorized = False
        cityscapes_eval.args.gtInstancesFile = os.path.join(self._temp_dir, "gtInstances.json")

        # These lines are adopted from
        # https://github.com/mcordts/cityscapesScripts/blob/master/cityscapesscripts/evaluation/evalInstanceLevelSemanticLabeling.py # noqa
        gt_dir = PathManager.get_local_path(self._metadata.gt_dir)
        groundTruthImgList = glob.glob(os.path.join(gt_dir, "*", "*_gtFine_instanceIds.png"))
        assert len(
            groundTruthImgList
        ), "Cannot find any ground truth images to use for evaluation. Searched for: {}".format(
            cityscapes_eval.args.groundTruthSearch
        )
        predictionImgList = []
        for gt in groundTruthImgList:
            predictionImgList.append(cityscapes_eval.getPrediction(gt, cityscapes_eval.args))
        results = cityscapes_eval.evaluateImgLists(
            predictionImgList, groundTruthImgList, cityscapes_eval.args
        )["averages"]

        ret = OrderedDict()
        ret["segm"] = {"AP": results["allAp"] * 100, "AP50": results["allAp50%"] * 100}
        self._working_dir.cleanup()
        return ret


class CityscapesSemSegEvaluator(CityscapesEvaluator):
    """
    Evaluate semantic segmentation results using cityscapes API.

    Note:
        * It does not work in multi-machine distributed training.
        * It contains a synchronization, therefore has to be used on all ranks.
        * Only the main process runs evaluation.
    """

    def process(self, inputs, outputs):
        from cityscapesscripts.helpers.labels import trainId2label

        for input, output in zip(inputs, outputs):
            file_name = input["file_name"]
            basename = os.path.splitext(os.path.basename(file_name))[0]
            pred_filename = os.path.join(self._temp_dir, basename + "_pred.png")

            output = output["sem_seg"].argmax(dim=0).to(self._cpu_device).numpy()
            pred = 255 * np.ones(output.shape, dtype=np.uint8)
            for train_id, label in trainId2label.items():
                if label.ignoreInEval:
                    continue
                pred[output == train_id] = label.id
            Image.fromarray(pred).save(pred_filename)

    def evaluate(self):
        comm.synchronize()
        if comm.get_rank() > 0:
            return
        # Load the Cityscapes eval script *after* setting the required env var,
        # since the script reads CITYSCAPES_DATASET into global variables at load time.
        import cityscapesscripts.evaluation.evalPixelLevelSemanticLabeling as cityscapes_eval

        self._logger.info("Evaluating results under {} ...".format(self._temp_dir))

        # set some global states in cityscapes evaluation API, before evaluating
        cityscapes_eval.args.predictionPath = os.path.abspath(self._temp_dir)
        cityscapes_eval.args.predictionWalk = None
        cityscapes_eval.args.JSONOutput = False
        cityscapes_eval.args.colorized = False

        # These lines are adopted from
        # https://github.com/mcordts/cityscapesScripts/blob/master/cityscapesscripts/evaluation/evalPixelLevelSemanticLabeling.py # noqa
        gt_dir = PathManager.get_local_path(self._metadata.gt_dir)
        groundTruthImgList = glob.glob(os.path.join(gt_dir, "*", "*_gtFine_labelIds.png"))
        assert len(
            groundTruthImgList
        ), "Cannot find any ground truth images to use for evaluation. Searched for: {}".format(
            cityscapes_eval.args.groundTruthSearch
        )
        predictionImgList = []
        for gt in groundTruthImgList:
            predictionImgList.append(cityscapes_eval.getPrediction(cityscapes_eval.args, gt))
        results = cityscapes_eval.evaluateImgLists(
            predictionImgList, groundTruthImgList, cityscapes_eval.args
        )
        ret = OrderedDict()
        ret["sem_seg"] = {
            "IoU": 100.0 * results["averageScoreClasses"],
            "iIoU": 100.0 * results["averageScoreInstClasses"],
            "IoU_sup": 100.0 * results["averageScoreCategories"],
            "iIoU_sup": 100.0 * results["averageScoreInstCategories"],
        }
        self._working_dir.cleanup()
        return ret


class ViperCityscapesSemSegEvaluator(CityscapesEvaluator):
    """
    Evaluate semantic segmentation results using cityscapes API.

    Note:
        * It does not work in multi-machine distributed training.
        * It contains a synchronization, therefore has to be used on all ranks.
        * Only the main process runs evaluation.
    """

    def process(self, inputs, outputs):
        from cityscapesScripts.cityscapesscripts.helpers.labels import trainId2label
        from detectron2.utils.labels_viper16 import label_viper, trainId2label
        for input, output in zip(inputs, outputs):
            file_name = input["file_name"]
            basename = os.path.splitext(os.path.basename(file_name))[0]
            pred_filename = os.path.join(self._temp_dir, basename + "_pred.png")
            output = output["sem_seg"].argmax(dim=0).to(self._cpu_device).numpy()
            pred = 255 * np.ones(output.shape, dtype=np.uint8)
            for train_id, label in trainId2label.items():
                if label.ignoreInEval:
                    continue
                pred[output == train_id] = label.id

            Image.fromarray(pred).save(pred_filename)

    def evaluate(self):
        comm.synchronize()
        if comm.get_rank() > 0:
            return
        # Load the Cityscapes eval script *after* setting the required env var,
        # since the script reads CITYSCAPES_DATASET into global variables at load time.
        import cityscapesScripts.cityscapesscripts.evaluation.evalPixelLevelSemanticLabeling as cityscapes_eval

        self._logger.info("Evaluating results under {} ...".format(self._temp_dir))

        # set some global states in cityscapes evaluation API, before evaluating
        cityscapes_eval.args.predictionPath = os.path.abspath(self._temp_dir)
        cityscapes_eval.args.predictionWalk = None
        cityscapes_eval.args.JSONOutput = False
        cityscapes_eval.args.colorized = False

        cityscapes_eval.args.span_window = 5


        # These lines are adopted from
        # https://github.com/mcordts/cityscapesScripts/blob/master/cityscapesscripts/evaluation/evalPixelLevelSemanticLabeling.py # noqa
        gt_dir = PathManager.get_local_path(self._metadata.gt_dir)
        if self.dataset_type == 'cityscapes':
            groundTruthImgList = glob.glob(os.path.join(gt_dir, "*", "*_gtFine_labelIds.png"))
            groundTruthImgList = sorted(groundTruthImgList) 
        else:
            groundTruthImgList = glob.glob(os.path.join(gt_dir, "*", "*"))
            groundTruthImgList = sorted(groundTruthImgList)
            groundTruthImgList_tmp = [] 

        #### Delete non-evaluated data from the fisrt for video evaluation
        start_idx = int(groundTruthImgList[0].split('/')[-1].split('_')[-1].split('.')[0])
        for x in range(len(groundTruthImgList)):
            if int(groundTruthImgList[x].split('/')[-1].split('_')[-1].split('.')[0]) > self.inf_start:
                #### Skip intervals for video evaluation
                if int(groundTruthImgList[x].split('/')[-1].split('_')[-1].split('.')[0]) % self.skip_interv == start_idx: 
                    groundTruthImgList_tmp.append(groundTruthImgList[x])
        groundTruthImgList = groundTruthImgList_tmp



        assert len(
            groundTruthImgList
        ), "Cannot find any ground truth images to use for evaluation. Searched for: {}".format(
            cityscapes_eval.args.groundTruthSearch
        )
        

        ##########  Prediction Img List: 0 / 5 / 10 / 15 ...
        predictionImgList = []
        for gt in groundTruthImgList:
            predictionImgList.append(cityscapes_eval.getPrediction(cityscapes_eval.args, gt))

        ################################################

        # results = cityscapes_eval.evaluateImgLists(
        #     predictionImgList, groundTruthImgList, cityscapes_eval.args
        # )

        # import pdb
        # pdb.set_trace()
        results = cityscapes_eval.evaluateImgLists_video(
            predictionImgList, groundTruthImgList, cityscapes_eval.args
        )
        # video_results_10 = cityscapes_eval.evaluateImgLists_video_10(
        #     predictionImgList, groundTruthImgList, cityscapes_eval.args
        # )
        # video_results_15 = cityscapes_eval.evaluateImgLists_video_15(
        #     predictionImgList, groundTruthImgList, cityscapes_eval.args
        # )


        ret = OrderedDict()
        # ret_video = OrderedDict()
        # ret_img_vid = OrderedDict()
        # import pdbdsf
        ret["sem_seg"] = {
            "IoU": 100.0 * results["averageScoreClasses"],
            "IoU_sup": 100.0 * results["averageScoreCategories"],
            "VIoU_5": 100.0 * results["averageScoreClasses_vid5"],
            "VIoU_sup_5": 100.0 * results["averageScoreCategories_vid5"],
            "VIoU_10": 100.0 * results["averageScoreClasses_vid10"],
            "VIoU_sup_10": 100.0 * results["averageScoreCategories_vid10"],
            "VIoU_15": 100.0 * results["averageScoreClasses_vid15"],
            "VIoU_sup_15": 100.0 * results["averageScoreCategories_vid15"],
        }
      
        ret["sem_seg_Vid"] = {
            "VIoU_total": (ret["sem_seg"]["VIoU_5"] + \
                          ret["sem_seg"]["VIoU_10"] + \
                          ret["sem_seg"]["VIoU_15"]) / 3,

            "VIoU_sup_total": (ret["sem_seg"]["VIoU_sup_5"] + \
                          ret["sem_seg"]["VIoU_sup_10"] + \
                          ret["sem_seg"]["VIoU_sup_15"]) / 3,
        }


        ret["sem_seg_ImgVid"] = {
            "ImgVid_IoU_Total": (ret["sem_seg_Vid"]["VIoU_total"] * 3 + \
                           ret["sem_seg"]["IoU"]) / 4,
            "ImgVid_sup_IoU_Total": (ret["sem_seg_Vid"]["VIoU_sup_total"] * 3 + \
                           ret["sem_seg"]["IoU_sup"]) / 4,
        }

        # ret.update(ret_img_vid)

        self._working_dir.cleanup()
        return ret
