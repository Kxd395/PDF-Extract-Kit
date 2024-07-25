from detectron2.utils.logger import setup_logger
setup_logger()
from modules.layoutlmv3.model_init import *

import time, math
class _DummyTimer:
    """A dummy timer that does nothing."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

class _Timer:
    """Timer."""

    def __init__(self, name):
        self.name = name
        self.count = 0
        self.mean = 0.0
        self.sum_squares = 0.0
        self.start_time = None

    def __enter__(self):
        self.start_time = time.time()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        elapsed_time = time.time() - self.start_time
        self.update(elapsed_time)
        self.start_time = None

    def update(self, elapsed_time):
        self.count += 1
        delta = elapsed_time - self.mean
        self.mean += delta / self.count
        delta2 = elapsed_time - self.mean
        self.sum_squares += delta * delta2

    def mean_elapsed(self):
        return self.mean

    def std_elapsed(self):
        if self.count > 1:
            variance = self.sum_squares / (self.count - 1)
            return math.sqrt(variance)
        else:
            return 0.0

class Timers:
    """Group of timers."""

    def __init__(self, activate=False):
        self.timers = {}
        self.activate = activate
    def __call__(self, name):
        if not self.activate:return _DummyTimer()
        if name not in self.timers:
            self.timers[name] = _Timer(name)
        return self.timers[name]

    def log(self, names=None, normalizer=1.0):
        """Log a group of timers."""
        assert normalizer > 0.0
        if names is None:
            names = self.timers.keys()
        print("Timer Results:")
        for name in names:
            mean_elapsed = self.timers[name].mean_elapsed() * 1000.0 / normalizer
            std_elapsed = self.timers[name].std_elapsed() * 1000.0 / normalizer
            space_num = " "*name.count('/')
            print(f"{space_num}{name}: {mean_elapsed:.2f}±{std_elapsed:.2f} ms")

def inference(
        self,
        batched_inputs: List[Dict[str, torch.Tensor]],
        detected_instances: Optional[List[Instances]] = None,
        do_postprocess: bool = True,
        timers = None
    ):
        """
        Run inference on the given inputs.

        Args:
            batched_inputs (list[dict]): same as in :meth:`forward`
            detected_instances (None or list[Instances]): if not None, it
                contains an `Instances` object per image. The `Instances`
                object contains "pred_boxes" and "pred_classes" which are
                known boxes in the image.
                The inference will then skip the detection of bounding boxes,
                and only predict other per-ROI outputs.
            do_postprocess (bool): whether to apply post-processing on the outputs.

        Returns:
            When do_postprocess=True, same as in :meth:`forward`.
            Otherwise, a list[Instances] containing raw network outputs.
        """
        assert not self.training
        
        with timers('inference/preprocess_image'):
            images = self.preprocess_image(batched_inputs)
        # features = self.backbone(images.tensor)
        with timers('inference/get_batch'):
            input = self.get_batch(batched_inputs, images)
        with timers('inference/get_features'):
            features = self.backbone(input)
        with timers('inference/merge_proposals'):
            if detected_instances is None:
                if self.proposal_generator is not None:
                    with timers('merge_proposals/compute_proposals'):
                        proposals, _ = self.proposal_generator(images, features, None)
                else:
                    assert "proposals" in batched_inputs[0]
                    proposals = [x["proposals"].to(self.device) for x in batched_inputs]
                with timers('inference/merge_proposals/roi_heads'):
                    results, _ = self.roi_heads(images, features, proposals, None)
            else:
                detected_instances = [x.to(self.device) for x in detected_instances]
                results = self.roi_heads.forward_with_given_boxes(features, detected_instances)
        with timers('inference/postprocess'):
            if do_postprocess:
                assert not torch.jit.is_scripting(), "Scripting is not supported for postprocess."
                results =  GeneralizedRCNN._postprocess(results, batched_inputs, images.image_sizes)
        return results

class Layoutlmv3_BatchPredictor(Layoutlmv3_Predictor):

    timers = Timers(False)

    def batch_predict(self, image_and_height_and_width,timers):
        with torch.no_grad():  # https://github.com/sphinx-doc/sphinx/issues/4258
            images,heights, widths = image_and_height_and_width
            inputs =[ {"image": image, "height": height, "width": width} for image, height, width in zip(images,heights, widths)]
            #inputs = {"image": images, "height": heights, "width": widths}
            predictions = inference(self.predictor.model,inputs,timers=timers)
            return predictions

    def __call__(self, image, ignore_catids=[]):
        with self.timers('inference'):
            outputslist = self.batch_predict(image, self.timers)
        use_old_bbox_collection = False
        if use_old_bbox_collection:
            with self.timers('wholepost'):
                page_layout_result_list = []
                for outputs in outputslist:
                    page_layout_result = {
                        "layout_dets": []
                    }
                    
                    # Convert tensor data to numpy arrays
                    with self.timers('wholepost/to_numpy'):
                        boxes = outputs["instances"].to("cpu")._fields["pred_boxes"].tensor.numpy()
                        labels = outputs["instances"].to("cpu")._fields["pred_classes"].numpy()
                        scores = outputs["instances"].to("cpu")._fields["scores"].numpy()
                    
                    with self.timers('wholepost/compute_mask'):
                        # Create a mask for filtering out the ignored categories
                        mask = np.isin(labels, ignore_catids, invert=True)
                    
                    with self.timers('wholepost/slicing'):
                        # Apply the mask to filter out the ignored categories
                        filtered_boxes = boxes[mask]
                        filtered_labels = labels[mask]
                        filtered_scores = scores[mask]
                    
                    with self.timers('wholepost/stack'):
                        # Collect the layout details
                        polys = np.column_stack([
                            filtered_boxes[:, 0], filtered_boxes[:, 1],
                            filtered_boxes[:, 2], filtered_boxes[:, 1],
                            filtered_boxes[:, 2], filtered_boxes[:, 3],
                            filtered_boxes[:, 0], filtered_boxes[:, 3]
                        ])
                    
                    with self.timers('wholepost/restack_layout'):
                        # Populate the layout_dets
                        for i in range(len(filtered_labels)):
                            page_layout_result["layout_dets"].append({
                                "category_id": filtered_labels[i],
                                "poly": polys[i].tolist(),
                                "score": filtered_scores[i]
                            })
                    
                    page_layout_result_list.append(page_layout_result)
        else:
            with self.timers('wholepost'):
                page_layout_result_list = []
                for outputs in outputslist:
                    page_layout_result = {
                        "layout_dets": []
                    }
                    instances = outputs["instances"]
                    # Convert tensor data to numpy arrays
                    with self.timers('wholepost/to_numpy1'):
                        boxes  = instances._fields["pred_boxes"].tensor
                        labels = instances._fields["pred_classes"]
                        scores = instances._fields["scores"]
                                    
                    with self.timers('wholepost/compute_mask'):
                        # Create a mask for filtering out the ignored categories
                        ignore_catids_tensor = torch.tensor(ignore_catids, device=labels.device)
                        mask = ~torch.isin(labels, ignore_catids_tensor)
                    
                    with self.timers('wholepost/slicing'):
                        # Apply the mask to filter out the ignored categories
                        filtered_boxes = boxes[mask]
                        filtered_labels = labels[mask]
                        filtered_scores = scores[mask]
                    
                    with self.timers('wholepost/to_numpy2'):
                        filtered_boxes  = filtered_boxes.cpu().numpy()
                        filtered_labels = filtered_labels.cpu().numpy()
                        filtered_scores = filtered_scores.cpu().numpy()
                    
                    with self.timers('wholepost/stack'):
                        # Collect the layout details
                        polys = np.column_stack([
                            filtered_boxes[:, 0], filtered_boxes[:, 1],
                            filtered_boxes[:, 2], filtered_boxes[:, 1],
                            filtered_boxes[:, 2], filtered_boxes[:, 3],
                            filtered_boxes[:, 0], filtered_boxes[:, 3]
                        ])
                    
                    with self.timers('wholepost/restack_layout'):
                        # Populate the layout_dets
                        for i in range(len(filtered_labels)):
                            page_layout_result["layout_dets"].append({
                                "category_id": filtered_labels[i],
                                "poly": polys[i].tolist(),
                                "score": filtered_scores[i]
                            })
                    
                    page_layout_result_list.append(page_layout_result)
        #self.timers.log()
        return page_layout_result_list



def get_layout_model(model_configs):
    model = Layoutlmv3_BatchPredictor(model_configs['model_args']['layout_weight'])
    return model