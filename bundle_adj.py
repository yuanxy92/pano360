"""Estimate the camera parameters with bundle adjustment."""
import os
from dataclasses import dataclass

import numpy as np
import cv2

from blend import intrinsics, SphProj


MAX_RESOLUTION = 1400

# bundle adjustment parameters
PARAMS_PER_CAMERA = 6
TERMS_PER_MATCH = 2


@dataclass
class Image:
    """Patch with all the informations for stitching."""

    img: np.ndarray
    rot: np.ndarray
    intr: np.ndarray
    range: tuple = (np.zeros(2), np.zeros(2))

    def hom(self):
        """Homography to normalized coordinates."""
        return self.rot.dot(np.linalg.inv(self.intr))

    def inv_hom(self):
        """Return inverse camera transform."""
        return self.intr.dot(self.rot.T)


def _hom(cm1, cm2):
    """Homography between two cameras."""
    return (cm1.intr.dot(cm1.rot)).dot(cm2.rot.T.dot(np.linalg.inv(cm2.intr)))


def _focal(v1, v2, d1, d2):
    """Get focal from two squared estimates."""
    if v1 < v2:
        v1, v2 = v2, v1
    if v1 > 0 and v2 > 0:
        return np.sqrt(v1 if abs(d1) > abs(d2) else v2)
    elif v1 > 0:
        return np.sqrt(v1)
    return 0


def _get_focal(hom):
    """Run on the homography and its inverse to get a valid estimate."""
    hom = hom.ravel()

    d1, d2 = hom[6]*hom[7], (hom[7] - hom[6])*(hom[7] + hom[6])
    v1 = -(hom[0]*hom[1] + hom[3]*hom[4]) / d1
    v2 = (hom[0]*hom[0] + hom[3]*hom[3] - hom[1]*hom[1] - hom[4]*hom[4]) / d2
    f1 = _focal(v1, v2, d1, d2)

    d1 = hom[0]*hom[3] + hom[1]*hom[4]
    d2 = hom[0]*hom[0] + hom[1]*hom[1] - hom[3]*hom[3] - hom[4]*hom[4]
    v1, v2 = -hom[2]*hom[5] / d1, (hom[5]*hom[5] - hom[2]*hom[2]) / d2
    f0 = _focal(v1, v2, d1, d2)

    return np.sqrt(f0*f1)


def get_focal(hom):
    """Estimate the focal lenght from the homography [1].

    References
    ----------
    [1] Szeliski, Richard, and Heung-Yeung Shum. "Creating full view panoramic
    image mosaics and environment maps." Proceedings of the 24th annual
    conference on Computer graphics and interactive techniques. 1997.
    """
    f_ = _get_focal(hom)
    return f_ if f_ else _get_focal(np.linalg.inv(hom))


def _cross_mat(vec):
    """Skew symm. matrix for cross product."""
    return np.array(
        [[0, -vec[2], vec[1]], [vec[2], 0, -vec[0]], [-vec[1], vec[0], 0]])


def rotation_to_mat(rad=np.random.randn(3)):
    """Create a rotation matrices from the exponential representation."""
    ang = np.linalg.norm(rad)
    cross = _cross_mat(rad / ang if ang else rad)

    return np.eye(3) + cross*np.sin(ang) + (1-np.cos(ang))*cross.dot(cross)


def mat_to_angle(rot):
    """Exponential representation from rotation matrix."""
    rad = np.array(
        [rot[2, 1]-rot[1, 2], rot[0, 2]-rot[2, 0], rot[1, 0]-rot[0, 1]])
    mod = np.linalg.norm(rad)

    if mod < 1e-7:
        rad = 0
    else:
        theta = np.arccos(np.clip((np.trace(rot)-1) / 2, -1, 1))
        rad *= (theta / mod)
    return rad


def to_rotation(rot):
    """Find the closest rotation in the Frobenious norm."""
    uu_, _, vv_ = np.linalg.svd(rot)
    rot = uu_.dot(vv_)
    if np.linalg.det(rot) < 0:
        rot *= -1   # no reflections
    return rot


def absolute_rotations(homs, kints):
    """Find camera rotation w.r.t. a reference given homographies."""
    rots = [to_rotation(np.linalg.inv(kint).dot(hom.dot(kint)))
            for hom, kint in zip(homs, kints)]

    # mid point as reference to reduce drift
    mid = len(rots) // 2
    rot_r = [rots[mid]]
    for rot in rots[mid+1:]:
        rot_r.append(rot.dot(rot_r[-1]))
    rot_l = [np.eye(3)]
    for rot in rots[:mid]:
        rot_l.append(rot.T.dot(rot_l[-1]))
    return rot_l[::-1] + rot_r


def straighten(rots):
    """Global rotation to have the x axis on the same plane."""
    cov = np.cov(np.stack([rot[0] for rot in rots], axis=-1))
    _, _, vv_ = np.linalg.svd(cov)
    v_y = vv_[2]
    v_z = np.sum(np.stack([rot[2] for rot in rots], axis=0), axis=0)
    v_x = np.cross(v_y, v_z)
    v_x /= np.linalg.norm(v_x)
    v_z = np.cross(v_x, v_y)

    rot_g = np.stack([v_x, v_y, v_z], axis=-1)
    return [rot.dot(rot_g) for rot in rots]


#
# Bundle adjustment
#

def initial_estimate(imgs, homs):
    """Find an initial estimate for estrinsics and intrinsics."""
    focals = [get_focal(hom) for hom in homs]
    homs = [np.linalg.inv(hom) for hom in homs]
    foc = np.median([f for f in focals])

    kints = [intrinsics(foc)] * len(imgs)
    rots = absolute_rotations(homs, kints)
    rots = straighten(rots)

    return [Image(im, rot, k) for im, rot, k in zip(imgs, rots, kints)]


def residuals():
    """Find estimation errors."""
    pass


def dr_dvi(rot):
    """Rotation derivative w.r.t. the exponential representation."""
    rad = mat_to_angle(rot)
    vsqr = np.sum(np.square(rad))
    if vsqr < 1e-14:
        return np.eye(3)

    ire = np.eye(3) - rot
    res = np.stack([_cross_mat(rad)*r for r in rad])
    res[0] += _cross_mat(np.cross(rad, ire[:, 0]))
    res[1] += _cross_mat(np.cross(rad, ire[:, 1]))
    res[2] += _cross_mat(np.cross(rad, ire[:, 2]))

    return res.dot(rot) / vsqr


def bundle_adjustment(regions, kpts, matches):
    """Refine the camera parameters."""
    m_offs = np.cumsum([0] + [len(m) for m in matches])
    n_match, m_offs = m_offs[-1], m_offs[:-1]
    # hack: create match dict for now
    mdict = {(i, i+1): m for i, m in enumerate(matches[:-1])}
    mdict[(len(matches)-1, 0)] = matches[-1]

    np_cam = PARAMS_PER_CAMERA * len(regions)
    print(n_match, np_cam)

    # jac = np.zeros((TERMS_PER_MATCH * n_match, np_cam), dtype="float32")
    # jac_t_jac = np.zeros((np_cam, np_cam)*2, dtype="float32")

    # drs = [dr_dvi(r.rot) for r in regions]  # cache rot derivatives
    # for idx, ((i, j), match) in enumerate(mdict.items()):
    #     pass

    return regions


#
# Reprojection
#

def _proj_img_range_border(rot, shape, kint):
    """Estimate the extent of the image after projection."""
    nel = 100
    height, width = shape

    side_x = np.linspace(0, width, nel)
    side_y = np.linspace(0, height, nel)
    borders = np.concatenate([
        np.stack([np.zeros(nel), side_y, np.ones(nel)], axis=1),
        np.stack([np.full(nel, width), side_y, np.ones(nel)], axis=1),
        np.stack([side_x, np.zeros(nel), np.ones(nel)], axis=1),
        np.stack([side_x, np.full(nel, height), np.ones(nel)], axis=1)])
    borders = borders - np.array([width/2, height/2, 0])

    pts = SphProj.hom2proj(rot.dot(np.linalg.inv(kint).dot(borders.T)).T)
    return np.min(pts, axis=0), np.max(pts, axis=0)   # range


def _proj_img_range_corners(shape, hom):
    """Estimate image extent from corners with check for angle wraparound."""
    height, width = shape
    pts = np.array([[-width/2, -height/2, 1], [width/2, -height/2, 1],
                    [-width/2, height/2, 1], [width/2, height/2, 1]])
    pts = SphProj.hom2proj(hom.dot(pts.T).T)

    xmin, xmax = min(pts[0, 0], pts[2, 0]), max(pts[1, 0], pts[3, 0])
    ymin, ymax = min(pts[0, 1], pts[1, 1]), max(pts[2, 1], pts[3, 1])
    if xmin > xmax:  # push to right
        xmax += 2 * np.pi
    if ymin > ymax:  # push on top
        ymax += np.pi

    return np.array([xmin, ymin]), np.array([xmax, ymax])


def estimate_resolution(regions):
    """Estimate the resolution of the final image."""
    min_r, max_r = zip(*[reg.range for reg in regions])
    min_r, max_r = np.min(min_r, axis=0), np.max(max_r, axis=0)
    size = max_r - min_r

    mid = regions[len(regions) // 2]   # central image
    im_shape = np.array(mid.img.shape[:2][::-1])
    resolution = (mid.range[1] - mid.range[0]) / im_shape

    max_side = np.max(size / resolution)
    if max_side > MAX_RESOLUTION:
        resolution *= max_side / MAX_RESOLUTION

    return resolution, (min_r, max_r)


def stitch(regions):
    """Stitch the images together."""
    for reg in regions:
        reg.range = _proj_img_range_corners(reg.img.shape[:2], reg.hom())

    resolution, im_range = estimate_resolution(regions)
    target = (im_range[1] - im_range[0]) / resolution

    shape = tuple(int(t) for t in np.round(target))[::-1]  # y,x order
    mosaic = np.zeros(shape + (3,), dtype=np.uint8)        # RGBA image
    for reg in regions:
        bottom = np.round((reg.range[0] - im_range[0])/resolution)
        top = np.round((reg.range[1] - im_range[0])/resolution)
        bottom, top = bottom.astype(np.int32), top.astype(np.int32)
        hh_, ww_ = reg.img.shape[:2]  # original image shape

        # find pixel coordinates
        y_i, x_i = np.indices((top[1]-bottom[1], top[0]-bottom[0]))
        x_i = (x_i + bottom[0]) * resolution[0] + im_range[0][0]
        y_i = (y_i + bottom[1]) * resolution[1] + im_range[0][1]
        xx_ = SphProj.proj2hom(np.stack([x_i, y_i], axis=-1).reshape(-1, 2))

        # transform to the original image coordinates
        xx_ = reg.inv_hom().dot(xx_.T).T.astype(np.float32)
        x_pr = xx_[:, :-1] / xx_[:, [-1]] + np.float32([ww_/2, hh_/2])
        x_pr = x_pr.reshape(top[1]-bottom[1], top[0]-bottom[0], -1)
        mask = (x_pr[..., 0] < 0) | (x_pr[..., 0] >= ww_) | \
               (x_pr[..., 1] < 0) | (x_pr[..., 1] >= hh_)
        x_pr[mask] = -1

        # paste only valid pixels
        warped = cv2.remap(reg.img, x_pr[:, :, 0], x_pr[:, :, 1],
                           cv2.INTER_AREA, borderMode=cv2.BORDER_CONSTANT)
        tile = mosaic[bottom[1]:top[1], bottom[0]:top[0]]
        mosaic[bottom[1]:top[1], bottom[0]:top[0]] = np.where(
            mask[..., None], tile, warped)

    return mosaic


def main():
    """Script entry point."""
    base_path = "../data/ppwwyyxx/CMU2"

    imgs = [cv2.imread(os.path.join(base_path, f"medium{i:02d}.JPG"))
            for i in range(13)]
    imgs = [cv2.resize(im, None, fx=0.5, fy=0.5) for im in imgs]

    arr = np.load("matches2.npz", allow_pickle=True)
    kpts, matches, homs = arr['kpts'], arr['matches'], arr['homs']

    regions = initial_estimate(imgs, homs)
    regions = bundle_adjustment(regions, kpts, matches)

    mosaic = stitch(regions)
    cv2.imshow("Mosaic", mosaic)

    if cv2.waitKey(0) & 0xff == 27:
        cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
