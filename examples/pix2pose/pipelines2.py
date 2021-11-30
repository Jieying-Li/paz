from paz.abstract import SequentialProcessor, Processor
from paz.pipelines import RandomizeRenderedImage as RandomizeRender
from paz.abstract.messages import Pose6D
from paz import processors as pr
from processors import (
    GetNonZeroArguments, GetNonZeroValues, ArgumentsToImagePoints2D,
    ImageToNormalizedDeviceCoordinates, Scale, SolveChangingObjectPnPRANSAC,
    ReplaceLowerThanThreshold)
from backend import build_cube_points3D
from processors import UnwrapDictionary
from processors import NormalizePoints2D
from backend import denormalize_points2D
from backend import draw_poses6D
from backend import draw_masks
from backend import draw_mask
from backend import draw_pose6D
from backend import normalize_points2D
from paz.backend.quaternion import rotation_vector_to_quaternion
from paz.backend.image import resize_image, show_image


class DomainRandomization(SequentialProcessor):
    """Performs domain randomization on a rendered image
    """
    def __init__(self, renderer, image_shape, image_paths, inputs_to_shape,
                 labels_to_shape, num_occlusions=1):
        super(DomainRandomization, self).__init__()
        H, W = image_shape[:2]
        self.add(pr.Render(renderer))
        self.add(pr.ControlMap(RandomizeRender(image_paths), [0, 1], [0]))
        self.add(pr.ControlMap(pr.NormalizeImage(), [0], [0]))
        self.add(pr.ControlMap(pr.NormalizeImage(), [1], [1]))
        self.add(pr.SequenceWrapper({0: inputs_to_shape},
                                    {1: labels_to_shape}))


class PredictRGBMask(SequentialProcessor):
    def __init__(self, model, epsilon=0.15):
        super(PredictRGBMask, self).__init__()
        self.add(pr.ResizeImage(model.input_shape[1:3]))
        self.add(pr.NormalizeImage())
        self.add(pr.ExpandDims(0))
        self.add(pr.Predict(model))
        self.add(pr.Squeeze(0))
        self.add(ReplaceLowerThanThreshold(epsilon))
        self.add(pr.DenormalizeImage())
        self.add(pr.CastImage('uint8'))


class RGBMaskToObjectPoints3D(SequentialProcessor):
    def __init__(self, object_sizes):
        super(RGBMaskToObjectPoints3D, self).__init__()
        self.add(GetNonZeroValues())
        self.add(ImageToNormalizedDeviceCoordinates())
        self.add(Scale(object_sizes / 2.0))


class RGBMaskToImagePoints2D(SequentialProcessor):
    def __init__(self, output_shape):
        super(RGBMaskToImagePoints2D, self).__init__()
        self.add(GetNonZeroArguments())
        self.add(ArgumentsToImagePoints2D())
        # self.add(NormalizePoints2D(output_shape))


class SolveChangingObjectPnP(SequentialProcessor):
    def __init__(self, camera_intrinsics, inlier_thresh=5, num_iterations=100):
        super(SolveChangingObjectPnP, self).__init__()
        self.MINIMUM_REQUIRED_POINTS = 4
        self.add(SolveChangingObjectPnPRANSAC(
            camera_intrinsics, inlier_thresh, num_iterations))


class Pix2Points(pr.Processor):
    def __init__(self, model, object_sizes, epsilon=0.15,
                 resize=True, draw=True):
        self.object_sizes = object_sizes
        self.predict_RGBMask = PredictRGBMask(model, epsilon)
        self.mask_to_points3D = RGBMaskToObjectPoints3D(self.object_sizes)
        self.mask_to_points2D = RGBMaskToImagePoints2D(model.output_shape[1:3])
        self.resize, self.draw = resize, draw
        self.wrap = pr.WrapOutput(
            ['image', 'points2D', 'points3D', 'RGB_mask'])

    def call(self, image):
        RGB_mask = self.predict_RGBMask(image)
        H, W, num_channels = image.shape
        if self.resize:
            RGB_mask = resize_image(RGB_mask, (W, H))
        points3D = self.mask_to_points3D(RGB_mask)
        points2D = self.mask_to_points2D(RGB_mask)
        points2D = normalize_points2D(points2D, W, H)
        if self.draw:
            image = draw_mask(image, points2D, points3D, self.object_sizes)
        return self.wrap(image, points2D, points3D, RGB_mask)


class Pix2Pose(pr.Processor):
    def __init__(self, model, object_sizes, camera, epsilon=0.15,
                 class_name=None, resize=True, draw=True):
        self.pix2points = Pix2Points(
            model, object_sizes, epsilon, resize, False)
        self.predict_pose = SolveChangingObjectPnP(camera.intrinsics)
        self.MIN_REQUIRED_POINTS = self.predict_pose.MINIMUM_REQUIRED_POINTS
        self.class_name = str(class_name) if class_name is None else class_name
        self.object_sizes = object_sizes
        self.cube_points3D = build_cube_points3D(*self.object_sizes)
        self.change_coordinates = pr.ChangeKeypointsCoordinateSystem()
        self.camera = camera
        self.draw = draw

    def call(self, image, box2D=None):
        results = self.pix2points(image)
        points2D, points3D = results['points2D'], results['points3D']
        H, W, num_channels = image.shape
        points2D = denormalize_points2D(points2D, H, W)
        if box2D is not None:
            points2D = self.change_coordinates(points2D, box2D)

        valid_num_points = len(points3D) > self.MIN_REQUIRED_POINTS
        if valid_num_points:
            success, rotation, translation = self.predict_pose(points3D,
                                                               points2D)
        if success and valid_num_points:
            quaternion = rotation_vector_to_quaternion(rotation)
            pose6D = Pose6D(quaternion, translation, self.class_name)
        else:
            pose6D = None

        if self.draw:
            image = draw_mask(image, points2D, points3D, self.object_sizes)
            image = draw_pose6D(image, pose6D, self.cube_points3D,
                                self.camera.intrinsics)
        results['pose6D'], results['image'] = pose6D, image
        return results


class EstimatePoseMasks(Processor):
    def __init__(self, detect, estimate_keypoints, camera, offsets, draw=True):
        """Pose estimation pipeline using keypoints.
        """
        super(EstimatePoseMasks, self).__init__()
        self.detect = detect
        self.estimate_keypoints = estimate_keypoints
        self.camera = camera
        self.draw = draw
        self.postprocess_boxes = SequentialProcessor(
            [pr.UnpackDictionary(['boxes2D']),
             pr.FilterClassBoxes2D(['035_power_drill']),
             # pr.FilterClassBoxes2D(['solar_panel']),
             pr.SquareBoxes2D(),
             pr.OffsetBoxes2D(offsets)])
        self.clip = pr.ClipBoxes2D()
        self.crop = pr.CropBoxes2D()
        self.change_coordinates = pr.ChangeKeypointsCoordinateSystem()
        self.predict_pose = SolveChangingObjectPnP(camera.intrinsics)
        self.unwrap = UnwrapDictionary(['points2D', 'points3D'])
        self.wrap = pr.WrapOutput(['image', 'boxes2D', 'poses6D'])
        self.draw_boxes2D = pr.DrawBoxes2D(detect.class_names)
        # self.draw_boxes2D = pr.DrawBoxes2D(['solar_panel'])
        self.object_sizes = self.estimate_keypoints.object_sizes
        self.cube_points3D = build_cube_points3D(*self.object_sizes)
        # affine_matrix = build_rotation_matrix_z(3.14156 / 6)
        # self.cube_points3D = np.matmul(affine_matrix, self.cube_points3D.T).T
        # 25000,
        # self.cube_points3D = self.cube_points3D + np.array([5000, 5000, 0])

    def call(self, image):
        from paz.abstract.messages import Box2D
        detections = self.detect(image)
        # detections = {'boxes2D': [Box2D([320, 280, 1300, 1060], 1.0, 'solar_panel')]}
        boxes2D = self.postprocess_boxes(detections)
        # boxes2D = self.postprocess_boxes(self.detect(image))
        boxes2D = self.clip(image, boxes2D)
        cropped_images = self.crop(image, boxes2D)
        poses6D, points = [], []
        for crop, box2D in zip(cropped_images, boxes2D):
            points2D, points3D = self.unwrap(self.estimate_keypoints(crop))
            points2D = denormalize_points2D(points2D, *crop.shape[0:2])
            points2D = self.change_coordinates(points2D, box2D)
            if len(points3D) < self.predict_pose.MINIMUM_REQUIRED_POINTS:
                continue
            success, rotation, translation = self.predict_pose(
                points3D, points2D)
            if success is False:
                continue
            quaternion = rotation_vector_to_quaternion(rotation)
            pose6D = Pose6D(quaternion, translation, box2D.class_name)
            poses6D.append(pose6D), points.append([points2D, points3D])
        if self.draw:
            image = self.draw_boxes2D(image, boxes2D)
            image = draw_masks(image, points, self.object_sizes)
            image = draw_poses6D(image, poses6D, self.cube_points3D,
                                 self.camera.intrinsics)
        return self.wrap(image, boxes2D, poses6D)



class Pix2Pose2(pr.Processor):
    def __init__(self, model, object_sizes, epsilon=0.15,
                 class_name=None, with_resize=True, draw=True):
        self.object_sizes = object_sizes
        self.predict_RGBMask = PredictRGBMask(model, epsilon)
        self.mask_to_points3D = RGBMaskToObjectPoints3D(self.object_sizes)
        self.mask_to_points2D = RGBMaskToImagePoints2D(model.output_shape[1:3])
        self.predict_pose = SolveChangingObjectPnP(camera.intrinsics)
        self.wrap = pr.WrapOutput(['image', 'points3D', 'points2D', 'RGB_mask'])
        self.with_resize = with_resize
        self.class_name = str(class_name) if class_name is None else class_name
        self.draw = draw

    def call(self, image):
        RGB_mask = self.predict_RGBMask(image)
        H, W, num_channels = image.shape
        if self.with_resize:
            RGB_mask = resize_image(RGB_mask, (W, H))
        points3D = self.mask_to_points3D(RGB_mask)
        points2D = self.mask_to_points2D(RGB_mask)
        points2D = normalize_points2D(points2D, (W, H))
        if len(points3D) < self.predict_pose.MINIMUM_REQUIRED_POINTS:
            pose6D = None
        success, rotation, translation = self.predict_pose(points3D, points2D)
        if success is False:
            pose6D = None
        quaternion = rotation_vector_to_quaternion(rotation)
        pose6D = Pose6D(quaternion, translation, self.class_name)
        if self.draw:
            image = draw_mask(image, points2D, points3D, self.object_sizes)
            image = draw_pose6D(image, pose6D, self.cube_points3D, self.camera.intrinsics)
        return self.wrap(image, points3D, points2D, RGB_mask)


class EstimatePoseMasks(Processor):
    def __init__(self, detect, estimate_keypoints, camera, offsets, draw=True):
        """Pose estimation pipeline using keypoints.
        """
        super(EstimatePoseMasks, self).__init__()
        self.detect = detect
        self.estimate_keypoints = estimate_keypoints
        self.camera = camera
        self.draw = draw
        self.postprocess_boxes = SequentialProcessor(
            [pr.UnpackDictionary(['boxes2D']),
             # pr.FilterClassBoxes2D(['035_power_drill']),
             pr.FilterClassBoxes2D(['solar_panel']),
             pr.SquareBoxes2D(),
             pr.OffsetBoxes2D(offsets)])
        self.clip = pr.ClipBoxes2D()
        self.crop = pr.CropBoxes2D()
        self.change_coordinates = pr.ChangeKeypointsCoordinateSystem()
        self.predict_pose = SolveChangingObjectPnP(camera.intrinsics)
        self.unwrap = UnwrapDictionary(['points2D', 'points3D'])
        self.wrap = pr.WrapOutput(['image', 'boxes2D', 'poses6D'])
        # self.draw_boxes2D = pr.DrawBoxes2D(detect.class_names)
        self.draw_boxes2D = pr.DrawBoxes2D(['solar_panel'])
        self.object_sizes = self.estimate_keypoints.object_sizes
        from backend import build_rotation_matrix_z
        import numpy as np
        self.cube_points3D = build_cube_points3D(*self.object_sizes)
        affine_matrix = build_rotation_matrix_z(3.14156 / 6)
        self.cube_points3D = np.matmul(affine_matrix, self.cube_points3D.T).T
        # 25000,
        # self.cube_points3D = self.cube_points3D + np.array([5000, 5000, 0])

    def call(self, image):
        from paz.abstract.messages import Box2D
        detections = self.detect(image)
        detections = {'boxes2D': [Box2D([320, 280, 1300, 1060], 1.0, 'solar_panel')]}
        boxes2D = self.postprocess_boxes(detections)
        # boxes2D = self.postprocess_boxes(self.detect(image))
        boxes2D = self.clip(image, boxes2D)
        cropped_images = self.crop(image, boxes2D)
        poses6D, points = [], []
        for crop, box2D in zip(cropped_images, boxes2D):
            points2D, points3D = self.unwrap(self.estimate_keypoints(crop))
            points2D = denormalize_points2D(points2D, *crop.shape[0:2])
            points2D = self.change_coordinates(points2D, box2D)
            if len(points3D) < self.predict_pose.MINIMUM_REQUIRED_POINTS:
                continue
            success, rotation, translation = self.predict_pose(
                points3D, points2D)
            if success is False:
                continue
            print('ROTATION', rotation.shape)
            quaternion = rotation_vector_to_quaternion(rotation)
            print('QUATERNION', quaternion.shape)
            pose6D = Pose6D(quaternion, translation, box2D.class_name)
            poses6D.append(pose6D), points.append([points2D, points3D])
        if self.draw:
            image = self.draw_boxes2D(image, boxes2D)
            image = draw_masks(image, points, self.object_sizes)
            image = draw_poses6D(image, poses6D, self.cube_points3D,
                                 self.camera.intrinsics)
        return self.wrap(image, boxes2D, poses6D)
