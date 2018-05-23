import datajoint as dj
import numpy as np
import torch

from torch.utils.data import DataLoader
from torch.nn import functional as F

from bl3d import train
from bl3d import datasets
from bl3d import transforms
from bl3d import params


schema = dj.schema('ecobost_bl3d', locals())


@schema
class Set(dj.Lookup):
    definition = """ # set where metrics are computed
    set:                    varchar(8)
    """
    contents = [['train'], ['val']]


@schema
class SegmentationMetrics(dj.Computed):
    definition = """ # compute cross validation metrics per pixel
    -> train.TrainedModel
    -> Set
    ---
    best_threshold:         float
    best_iou:               float
    best_f1:                float
    """
    class ThresholdSelection(dj.Part):
        definition= """ # all thresholds tried
        -> master
        ---
        thresholds:         blob            # all thresholds tried
        tps:                blob            # true positives
        fps:                blob            # false positives
        tns:                blob            # true negatives
        fns:                blob            # false negatives
        accuracies:         blob            # accuracy at each threshold
        precisions:         blob            # precision at each threshold
        recalls:            blob            # recall/sensitivity at each threshold
        specificities:      blob            # specificity at each threshold
        ious:               blob            # iou at each threshold
        f1s:                blob            # F-1 score at each threshold
        """

    def make(self, key):
        print('Evaluating', key)

        # Get model
        net = train.TrainedModel.load_model(key)

        # Move model to GPU
        net.cuda()
        net.eval()

        # Get dataset
        examples = (train.Split() & key).fetch1('{}_examples'.format(key['set']))
        dataset = datasets.SegmentationDataset(examples, transforms.ContrastNorm(), (params.TrainingParams() & key).fetch1('enhanced_input'))
        dataloader = DataLoader(dataset, num_workers=2, pin_memory=True)

        # Iterate over different probability thresholds
        thresholds = np.linspace(0, 1, 33)
        tps = []
        fps = []
        tns = []
        fns = []
        accuracies = []
        precisions = []
        recalls = []
        specificities = []
        ious = []
        f1s = []
        for threshold in thresholds:
            print('Threshold: {}'.format(threshold))

            confusion_matrix = np.zeros(4) # tp, fp, tn, fn
            with torch.no_grad():
                for image, label in dataloader:
                    # Compute prediction (heatmap of probabilities)
                    output = forward_on_big_input(net, image.cuda())
                    prediction = F.softmax(output, dim=1) # 1 x num_classes x depth x height x width

                    # Threshold prediction to create segmentation
                    segmentation = prediction[0, 1].cpu().numpy() > threshold

                    # Accumulate confusion matrix values
                    confusion_matrix += compute_confusion_matrix(segmentation, label.numpy())

            # Calculate metrics
            metrics = compute_metrics(*confusion_matrix)

            # Collect results
            tps.append(confusion_matrix[0])
            fps.append(confusion_matrix[1])
            tns.append(confusion_matrix[2])
            fns.append(confusion_matrix[3])
            accuracies.append(metrics[2])
            precisions.append(metrics[5])
            recalls.append(metrics[6])
            specificities.append(metrics[4])
            ious.append(metrics[0])
            f1s.append(metrics[1])

            print('IOU:', metrics[0])

        # Insert
        best_iou = max(ious)
        best_threshold = thresholds[ious.index(best_iou)]
        best_f1 = f1s[ious.index(best_iou)]
        self.insert1({**key, 'best_threshold': best_threshold, 'best_iou': best_iou,
                      'best_f1': best_f1})

        threshold_metrics = {**key, 'thresholds': thresholds, 'tps': tps, 'fps': fps,
                             'tns': tns, 'fns':fns, 'accuracies': accuracies,
                             'precisions': precisions, 'recalls': recalls,
                             'specificities': specificities, 'ious': ious, 'f1s': f1s}
        self.ThresholdSelection().insert1(threshold_metrics)


@schema
class DetectionMetrics(dj.Computed):
    definition = """ # object detection metrics
    -> train.TrainedModel
    -> Set
    ---
    map:            float       # mean average precision over all acceptance IOUs (same as COCO's mAP)
    f1:             float       # mean F1 over all acceptance IOUs
    map_50:         float       # mean average precision at IOU = 0.5 (default in Pascal VOC)
    map_75:         float       # mean average precision at IOU = 0.75 (more strict)
    f1_50:          float       # F-1 at acceptance IOU = 0.5
    f1_75:          float       # F-1 at acceptance IOU = 0.75
    """
    class PerIOU(dj.Part):
        definition = """ # some metrics computed at a single acceptance IOUs
        -> master
        iou:                float       # acceptance iou used
        ---
        tps:                int         # true positives
        fps:                int         # false positives
        fns:                int         # false negatives
        accuracy:           float
        precision:          float
        recall:             float       # recall/sensitivity
        map:                float
        f1:                 float       # F-1 score
        """

    def make(self, key):
        print('Evaluating', key)

        # Get model
        net = train.TrainedModel.load_model(key)

        # Move model to GPU
        net.cuda()
        net.eval()

        # true negatives = 0
        pass

    """ mAP as computed by COCO:
        for each class
            for each image
                Predict k bounding boxes and k confidences
                Order by decreasing confidence
                for each bbox
                    for each acceptance_iou in [0.5, 0.55, 0.6, ..., 0.85, 0.9, 0.95]
                        Find the highest IOU ground truth box that has not been assigned yet
                        if highest iou > acceptance_iou
                            Save whether bbox is a match (and with whom it matches)
                    accum results over all acceptance ious
                accum results over all bboxes
            accum results over all images

            Reorder by decreasing confidence
            for each acceptance_iou in [0.5, 0.55, 0.6, ..., 0.85, 0.9, 0.95]:
                Compute precision and recall at each example
                for r in 0, 0.1, 0.2, ..., 1:
                    find precision at r as max(prec) at recall >= r
                average all 11 precisions -> average precision at detection_iou
            average all aps -> average precision
        average over all clases -> mean average precision
    """

def _prob2labels(pred):
    """ Transform voxelwise probabilities from a segmentation to instances.

    Pretty ad hoc. Bunch of numbers were manually chosen.

    Arguments:
        pred: Array with predicted probabilities.

    Returns:
        Array with same shape as pred with zero for background and positive integers for
            each predicted instance.
    """
    from skimage import filters, feature, morphology, measure, segmentation
    from scipy import ndimage

    # Find good binary threshold (may be a bit lower than best possible IOU, catches true cells that weren't labeled)
    thresh = filters.threshold_otsu(pred)

    # Find local maxima in the prediction heatmap
    smooth_pred = ndimage.gaussian_filter(pred, 1)
    peaks = feature.peak_local_max(smooth_pred, min_distance=4, threshold_abs=thresh,
                                   indices=False)
    markers = morphology.label(peaks)

    # Separate into instances based on distance
    thresholded = pred > thresh
    filled = morphology.remove_small_objects(morphology.remove_small_holes(thresholded), 65) # volume of sphere with diameter 5
    distance = ndimage.distance_transform_edt(filled)
    distance += 1e-7 * np.random.random(distance.shape) # to break ties
    label = morphology.watershed(-distance, markers, mask=filled)
    print(label.max(), 'initial cells')

    # Remove masks that are too small or too large
    label = morphology.remove_small_objects(label, 65)
    too_large = [p.label for p in measure.regionprops(label) if p.area > 4189]
    for label_id in too_large:
        label[label == label_id] = 0 # set to background
    label, _, _ = segmentation.relabel_sequential(label)
    print(label.max(), 'final cells')

    return label


def find_matches(labels, prediction):
    """ Find all labels that intersect with a given predicted mask.

    Arguments:
        labels: Array with zeros for background and positive integers for each ground
            truth object in the volume.
        prediction: Boolean array with ones for the predicted mask. Same shape as labels.

    Returns:
        List of (label, iou) pairs sorted by decreasing IOU.
    """
    ious = []
    for l in np.unique(labels[prediction]):
        label = labels == l
        iou = np.sum(np.logical_and(label, prediction)) / np.sum(np.logical_or(label, prediction))
        ious.append((l, iou))
    ious = sorted(ious, key=lambda x: x[1], reverse=True)

    return ious


def forward_on_big_input(net, volume, max_size=256, padding=32, out_channels=2):
    """ Passes a big volume through a network dividing it in chunks.

    Arguments:
        net: A pytorch network.
        volume: The input to the network (num_examples x num_channels x d1 x d2 x ...).
        max_size: An int or tuple of ints. Maximum input size for every volume dimension.
        pad_amount: An int or tuple of ints. Amount of padding performed by the network.
            We discard an edge of this size out of chunks in the middle of the FOV to
            avoid padding effects. Better to overestimate.
        out_channels: Number of channels in the output.

    Note:
        Assumes net and volume are in the same device (usually both in GPU).
        If net is in train mode, each chunk will be batch normalized with diff parameters.
    """
    import itertools

    # Get some params
    spatial_dims = volume.dim() - 2 # number of dimensions after batch and channel

    # Basic checks
    listify = lambda x: [x] * spatial_dims if np.isscalar(x) else list(x)
    padding = [int(round(x)) for x in listify(padding)]
    max_size = [int(round(x)) for x in listify(max_size)]
    if len(padding) != spatial_dims or len(max_size) != spatial_dims:
        msg = ('padding and max_size should be a single integer or a sequence of the '
               'same length as the number of spatial dimensions in the volume.')
        raise ValueError(msg)
    if np.any(2 * np.array(padding) >= np.array(max_size)):
        raise ValueError('Padding needs to be smaller than half max_size.')

    # Evaluate input chunk by chunk
    output = torch.zeros(volume.shape[0], out_channels, *volume.shape[2:])
    for initial_coords in itertools.product(*[range(p, d, s - 2 * p) for p, d, s in
                                              zip(padding, volume.shape[2:], max_size)]):
        # Cut chunk (it starts at coord - padding)
        cut_slices = [slice(c - p, c - p + s) for c, p, s in zip(initial_coords, padding, max_size)]
        chunk = volume[(..., *cut_slices)]

        # Forward
        out = net(chunk)

        # Assign to output dropping padded amount (special treat for first chunk)
        output_slices = [slice(0 if sl.start == 0 else c, sl.stop) for c, sl in zip(initial_coords, cut_slices)]
        out_slices = [slice(0 if sl.start == 0 else p, None) for p, sl in zip(padding, cut_slices)]
        output[(..., *output_slices)] = out[(..., *out_slices)]

    return output


def compute_confusion_matrix(segmentation, label):
    """Confusion matrix for a single image: # of pixels in each category.

    Arguments:
        segmentation: Boolean array. Predicted segmentation.
        label: Boolean array. Expected segmentation.

    Returns:
        A quadruple with true positives, false positives, true negatives and false
            negatives
    """
    true_positive = np.sum(np.logical_and(segmentation, label))
    false_positive = np.sum(np.logical_and(segmentation, np.logical_not(label)))
    true_negative = np.sum(np.logical_and(np.logical_not(segmentation), np.logical_not(label)))
    false_negative = np.sum(np.logical_and(np.logical_not(segmentation), label))

    return (true_positive, false_positive, true_negative, false_negative)


def compute_metrics(true_positive, false_positive, true_negative, false_negative):
    """ Computes a set of different metrics given the confusion matrix values.

    Arguments:
        true_positive: Number of true positive examples/pixels.
        false_positive: Number of false positive examples/pixels.
        true_negative: Number of true negative examples/pixels.
        false_negative: Number of false negative examples/pixels.

    Returns:
        A septuple with IOU, F-1 score, accuracy, sensitivity, specificity, precision and
            recall.
    """
    epsilon = 1e-7 # To avoid division by zero

    # Evaluation metrics
    accuracy = (true_positive + true_negative) / (true_positive + true_negative +
                                                  false_positive + false_negative + epsilon)
    sensitivity = true_positive / (true_positive + false_negative + epsilon)
    specificity = true_negative / (false_positive + true_negative + epsilon)
    precision = true_positive / (true_positive + false_positive + epsilon)
    recall = sensitivity
    iou = true_positive / (true_positive + false_positive + false_negative + epsilon)
    f1 = (2 * precision * recall) / (precision + recall + epsilon)

    return (iou, f1, accuracy, sensitivity, specificity, precision, recall)