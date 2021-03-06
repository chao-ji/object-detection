from abc import ABCMeta
from abc import abstractmethod

import numpy as np

from detection.metrics import np_box_ops
from detection.metrics import coco_helpers
from detection.utils import metrics_utils as utils


class DetectionMetricsCalculator(object):
  """Abstract base class for computing object detection metrics.
  """

  __metaclass__ = ABCMeta

  @abstractmethod
  def update_per_image_result(self):
    """Updates internal accumulators with per-image groundtruth and detection
    results.

    To be implemented by subclasses.    
    """
    pass

  @abstractmethod
  def calculate_metrics(self):
    """Calculates object detection metrics.

    To be implemented by subclasses.
    """
    pass


class PascalVocMetricsCalculator(DetectionMetricsCalculator):
  """Computes PASCAL VOC object detection metrics (mean Average Precision).
  """
  def __init__(self, num_classes, class_indices):
    """Constructor.

    Args:
      num_classes: int scalar, num of classes.
      class_indices: list of ints, the class indices starting at 1.
    """
    self._num_classes = num_classes
    self._class_indices = class_indices
    self._scores = [[] for _ in range(num_classes)]
    self._tp_fp_labels = [[] for _ in range(num_classes)]
    self._num_gt_instances = np.zeros(num_classes, dtype=float)

  @property
  def num_classes(self):
    return self._num_classes

  def update_per_image_result(self, result_dict):
    """Updates internal accumulators with per-image groundtruth and detection 
    results. 

    Args:
      result_dict: a dict mapping from field names to numpy arrays, holding
        the following entries:
        { 'boxes' float numpy array of shape [num_detections, 4],
          'classes' int numpy array of shape [num_detections],
          'scores' float numpy array of shape [num_detections]}
      gt_boxes: numpy array of shape [num_gt_boxes, 4], holding the 
        absolute groundtruth box coordinates in pixels.
      gt_labels: numpy array of shape [num_gt_boxes], holding 
        the indices of groundtruth box labels (starting from 1).

    Returns:
      None
    """
    gt_boxes = result_dict['gt_boxes']
    gt_labels = result_dict['gt_labels']

    gt_labels -= 1
    for label in gt_labels:
      self._num_gt_instances[label] += 1

    scores = [[] for _ in range(self._num_classes)]
    tp_fp_labels = [[] for _ in range(self._num_classes)]
    
    for i in range(self._num_classes):
      if i + 1 not in self._class_indices:
        continue
      det_indices = (result_dict['classes'] - 1 == i)
      gt_indices = gt_labels == i
      scores[i] = result_dict['scores'][det_indices]

      if not det_indices.any():
        scores[i] = np.array([], dtype=float)
        tp_fp_labels[i] = np.array([], dtype=bool)
        continue

      if not gt_indices.any():
        tp_fp_labels[i] = np.zeros(scores[i].shape[0], dtype=bool)
        continue

      iou = np_box_ops.iou(
          result_dict['boxes'][det_indices], gt_boxes[gt_indices])

      scores[i] = scores[i].astype(np.float64)
      tp_fp_labels[i] = np.zeros(scores[i].shape[0], dtype=bool)

      max_overlap_gt_ids = np.argmax(iou, axis=1)
      is_gt_box_detected = np.zeros(iou.shape[1], dtype=bool)

      for j in range(scores[i].shape[0]):
        gt_id = max_overlap_gt_ids[j]
        if iou[j, gt_id] >= 0.5:
          if not is_gt_box_detected[gt_id]:
            tp_fp_labels[i][j] = True
            is_gt_box_detected[gt_id] = True

      tp_fp_labels[i] = tp_fp_labels[i].astype(float)

    for i in range(self._num_classes):
      if i + 1 not in self._class_indices:
        continue
      if scores[i].shape[0] > 0:
        self._scores[i] = np.concatenate(
            [self._scores[i], scores[i]])
        self._tp_fp_labels[i] = np.concatenate(
            [self._tp_fp_labels[i], tp_fp_labels[i]])

  def calculate_metrics(self):
    """Calculates object detection metrics.

    Returns:
      average_precisions: list of floats, holding average precision per class.
    """
    average_precisions = {} 
    for i in range(self._num_classes):
      if i + 1 not in self._class_indices:
        continue

      scores = self._scores[i]
      tp_fp_labels = self._tp_fp_labels[i]
      precision, recall = utils.compute_precision_recall(
          scores, tp_fp_labels, self._num_gt_instances[i])
      average_precision = utils.compute_average_precision(precision, recall)

      average_precisions[i + 1] = average_precision
    return average_precisions


class MscocoMetricsCalculator(DetectionMetricsCalculator):
  """Computes COCO object detection metrics.
  """
  def __init__(self,
               categories,
               class_indices,
               evaluate_mask=False):
    """Constructor.

    Args:
      categories: a list of dict, each having two entries 'id' and 'name'.
        e.g., [{'id': 1, 'name': 'label_1'}, {'id': 2, 'name': 'label_2'}, ...]
      class_indices: list of ints, the class indices starting at 1.
    """
    self._categories = categories
    self._class_indices = class_indices
    self._evaluate_mask = evaluate_mask

    self._category_id_set = set([category['id']
        for category in self._categories])
    if evaluate_mask is True:
      self._image_id_to_mask_shape_map = {}

    # accumulators
    self._groundtruth_list = []
    self._detections_list = []
    self._image_id = 0
    self._annotation_id = 1

  def update_per_image_result(self, result_dict):
    """Updates internal accumulators with per-image groundtruth and detection 
    results.

    Args:
      result_dict: a dict mapping from field names to numpy arrays, holding
        the following entries:
        { 'boxes' float numpy array of shape [num_detections, 4],
          'classes' int numpy array of shape [num_detections],
          'scores' float numpy array of shape [num_detections]}
      gt_boxes: numpy array of shape [num_gt_boxes, 4], holding the 
        absolute groundtruth box coordinates in pixels.
      gt_labels: numpy array of shape [num_gt_boxes], holding 
        the indices of groundtruth box labels (starting from 1).

    Returns:
      None
    """
    valid_indices = [i in self._class_indices for i in result_dict['classes']]
    if self._evaluate_mask:
      result_dict['masks'] = result_dict['masks'][valid_indices]
    else:
      result_dict['boxes'] = result_dict['boxes'][valid_indices]
    result_dict['classes'] = result_dict['classes'][valid_indices]
    result_dict['scores'] = result_dict['scores'][valid_indices]

    gt_boxes = result_dict['gt_boxes']
    gt_labels = result_dict['gt_labels']
    gt_masks = None
    if 'gt_masks' in result_dict:
      gt_masks = result_dict['gt_masks']

    # groundtruth
    self._groundtruth_list.extend(coco_helpers.convert_groundtruth_to_coco_format(
        image_id=self._image_id,
        next_annotation_id=self._annotation_id,
        category_id_set=self._category_id_set,
        groundtruth_boxes=gt_boxes,
        groundtruth_classes=gt_labels,
        groundtruth_masks=gt_masks))
    self._annotation_id += gt_boxes.shape[0]
    if self._evaluate_mask:
      self._image_id_to_mask_shape_map[self._image_id] = gt_masks.shape

    # detections
    if self._evaluate_mask:
      self._detections_list.extend(
          coco_helpers.convert_mask_detections_to_coco_format(
              image_id=self._image_id,
              category_id_set=self._category_id_set,
              detection_masks=result_dict['masks'],
              detection_scores=result_dict['scores'],
              detection_classes=result_dict['classes']))
    else:
      self._detections_list.extend(
          coco_helpers.convert_box_detections_to_coco_format(
              image_id=self._image_id,
              category_id_set=self._category_id_set,
              detection_boxes=result_dict['boxes'],
              detection_scores=result_dict['scores'],
              detection_classes=result_dict['classes']))

    self._image_id += 1

  def calculate_metrics(self):
    """Calculates object detection metrics.

    Returns:
      metrics: a dict mapping from metrics names (string) to metrics values
        (float). 
    """
    if self._evaluate_mask:
      images = [{'id': image_id, 'height': shape[1], 'width': shape[2]}
          for image_id, shape in self._image_id_to_mask_shape_map.items()]
      detection_type, iou_type, metric_name = 'segmentation', 'segm', 'Mask'
    else:
      images = [{'id': image_id} for image_id in range(self._image_id)]
      detection_type, iou_type, metric_name = 'bbox', 'bbox', 'Box'

    groundtruth_dict = {
        'annotations': self._groundtruth_list,
        'images': images,
        'categories': self._categories
    }

    coco_wrapped_groundtruth = coco_helpers.COCOWrapper(
        groundtruth_dict, detection_type=detection_type)
    coco_wrapped_detections = coco_wrapped_groundtruth.LoadAnnotations(
        self._detections_list)
    evaluator = coco_helpers.COCOEvalWrapper(
        coco_wrapped_groundtruth, 
        coco_wrapped_detections, 
        agnostic_mode=False, 
        iou_type=iou_type)
    metrics, per_category_ap = evaluator.ComputeMetrics(
        include_metrics_per_category=False,
        all_metrics_per_category=False)
    metrics.update(per_category_ap)

    metrics = {metric_name + key: value for key, value in metrics.items()}
    return metrics


def validate_boxes(boxes):
  """Makes sure that input box coordinates are valid values.

  Args:
    boxes: 2-D numpy array of shape [num_boxes, 4], holding absolute box 
      coordinates ymin, xmin, ymax, xmax in pixels.

  Raise:
    ValueError if box coordinates are not valid.
  """
  y_valid_indicators = np.logical_and(0.0 <= boxes[:, 0],
                                      boxes[:, 0] < boxes[:, 2])
  x_valid_indicators = np.logical_and(0.0 <= boxes[:, 1],  
                                      boxes[:, 1] < boxes[:, 3])

  if not np.all(np.logical_and(y_valid_indicators, x_valid_indicators)):
    raise ValueError(
        'box coordinates must be valid: 0 <= ymin(xmin) < ymax(xmax)')
