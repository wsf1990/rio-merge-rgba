import logging
import math
import os
import numpy as np
import rasterio
import click
from rasterio.transform import Affine
from rasterio._base import get_index  # get_window

from cligj import files_inout_arg, format_opt
from rasterio.rio.helpers import resolve_inout
from rasterio.rio import options

logger = logging.getLogger('merge_rgba')


@click.command(short_help="Merge a stack of raster datasets.")
@files_inout_arg
@options.output_opt
@format_opt
@options.bounds_opt
@options.resolution_opt
@click.option('--force-overwrite', '-f', 'force_overwrite', is_flag=True,
              type=bool, default=False,
              help="Do not prompt for confirmation before overwriting output "
                   "file")
@click.option('--precision', type=int, default=7,
              help="Number of decimal places of precision in alignment of "
                   "pixels")
@options.creation_options
def merge_rgba(files, output, driver, bounds, res, force_overwrite,
               precision, creation_options):

    output, files = resolve_inout(files=files, output=output)

    if os.path.exists(output) and not force_overwrite:
        raise click.ClickException(
            "Output exists and won't be overwritten without the "
            "`-f` option")

    sources = [rasterio.open(f) for f in files]
    merge_rbga_tool(sources, output, bounds=bounds, res=res,
                    precision=precision)


def merge_rbga_tool(sources, outtif, bounds=None, res=None, precision=7):
    """A windowed, top-down approach to merging.
    For each block window, it loops through the sources,
    reads the corresponding source window until the block
    is filled with data or we run out of sources.

    Uses more disk IO but is faster* and
    consumes significantly less memory

    * The read efficiencies comes from using
    RGBA tifs where we can assume band 4 is the sole
    determinant of nodata. This avoids the use of
    expensive masked reads but, of course, limits
    what data can used. Hence merge_rgba.
    """
    first = sources[0]
    first_res = first.res
    nodataval = first.nodatavals[0]
    dtype = first.dtypes[0]
    profile = first.profile
    profile.pop('affine')

    # Extent from option or extent of all inputs.
    if bounds:
        dst_w, dst_s, dst_e, dst_n = bounds
    else:
        # scan input files.
        # while we're at it, validate assumptions about inputs
        xs = []
        ys = []
        for src in sources:
            left, bottom, right, top = src.bounds
            xs.extend([left, right])
            ys.extend([bottom, top])
            if src.profile['count'] != 4:  # TODO, how to test for alpha?
                raise ValueError("Inputs must be 4-band RGBA rasters")
        dst_w, dst_s, dst_e, dst_n = min(xs), min(ys), max(xs), max(ys)
    logger.debug("Output bounds: %r", (dst_w, dst_s, dst_e, dst_n))
    output_transform = Affine.translation(dst_w, dst_n)
    logger.debug("Output transform, before scaling: %r", output_transform)

    # Resolution/pixel size.
    if not res:
        res = first_res
    elif not np.iterable(res):
        res = (res, res)
    elif len(res) == 1:
        res = (res[0], res[0])
    output_transform *= Affine.scale(res[0], -res[1])
    logger.debug("Output transform, after scaling: %r", output_transform)

    # Compute output array shape. We guarantee it will cover the output
    # bounds completely.
    output_width = int(math.ceil((dst_e - dst_w) / res[0]))
    output_height = int(math.ceil((dst_n - dst_s) / res[1]))

    # Adjust bounds to fit.
    dst_e, dst_s = output_transform * (output_width, output_height)
    logger.debug("Output width: %d, height: %d", output_width, output_height)
    logger.debug("Adjusted bounds: %r", (dst_w, dst_s, dst_e, dst_n))

    profile['transform'] = output_transform
    profile['height'] = output_height
    profile['width'] = output_width

    # TODO nodata strategy to include non RGBA sources
    nodataval = 0
    profile['nodata'] = nodataval

    # create destination file
    with rasterio.open(outtif, 'w', **profile) as dstrast:

        for idx, dst_window in dstrast.block_windows():

            left, bottom, right, top = dstrast.window_bounds(dst_window)
            blocksize = ((dst_window[0][1] - dst_window[0][0]) *
                         (dst_window[1][1] - dst_window[1][0]))

            # initialize array destined for the block
            dst_count = first.count
            dst_rows, dst_cols = tuple(b - a for a, b in dst_window)
            dst_shape = (dst_count, dst_rows, dst_cols)
            logger.debug("Temp shape: %r", dst_shape)
            dstarr = np.zeros(dst_shape, dtype=dtype)

            # Read up srcs until
            # a. everything is data; i.e. no nodata
            # b. no sources left
            for src in sources:
                # The full_cover behavior is problematic here as it includes
                # extra pixels along the bottom right when the sources are
                # slightly misaligned
                #
                # src_window = get_window(left, bottom, right, top,
                #                         src.affine, precision=precision)
                #
                # With rio merge this just adds an extra row, but when the 
                # imprecision occurs at each block, you get artifacts

                # Alternative, custom get_window using rounding
                window_start = get_index(
                    left, top, src.affine, op=round, precision=precision)
                window_stop = get_index(
                    right, bottom, src.affine, op=round, precision=precision)
                src_window = tuple(zip(window_start, window_stop))

                temp = np.zeros(dst_shape, dtype=dtype)
                temp = src.read(out=temp, window=src_window,
                                boundless=True, masked=False)

                # pixels without data yet are available to write
                write_region = (dstarr[3] == 0)  # 0 is nodata
                np.copyto(dstarr, temp, where=write_region)

                # check if dest has any nodata pixels available
                if np.count_nonzero(dstarr[3]) == blocksize:
                    break

            dstrast.write(dstarr, window=dst_window)

    return output_transform


if __name__ == "__main__":
    merge_rgba()
