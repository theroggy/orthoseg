# -*- coding: utf-8 -*-
"""
Module with high-level operations to segment images.
"""

from concurrent import futures
import logging
import os
import glob
import datetime
import time
import math

import keras as kr
import numpy as np
import pandas as pd
import rasterio as rio
import rasterio.plot as rio_plot

import orthoseg.model.model_factory as mf
import orthoseg.model.model_helper as mh
import orthoseg.postprocess_predictions as postp

#-------------------------------------------------------------
# First define/init some general variables/constants
#-------------------------------------------------------------
# Get a logger...
logger = logging.getLogger(__name__)
#logger.setLevel(logging.DEBUG)

#-------------------------------------------------------------
# The real work
#-------------------------------------------------------------

def predict_dir(model,
                input_image_dir: str,
                output_base_dir: str,         
                border_pixels_to_ignore: int = 0,                
                projection_if_missing: str = None,
                input_mask_dir: str = None,
                batch_size: int = 16,                
                evaluate_mode: bool = False,
                force: bool = False):
    """
    Create a prediction for all the images in the directories specified 
    using the model specified.
    
    If evaluate_mode is False, the output folder(s) will contain:
         * the "raw" prediction for every image (if there are white pixels)
         * a geojson with the vectorized prediction, with a column "onborder"
           for each feature that is 1 if the feature is on the border of the
           tile, taking the border_pixels_to_ignore in account if applicable.
           This columns can be used to speed up union operations afterwards, 
           because only features on the border of tiles need to be unioned.
    
    If evaluate_mode is True, the results will all be put in the root of the 
    output folder, and the following files will be outputted:
        * the original image
        * the mask that was provided, if available
        * the "raw" prediction
        * a "cleaned" version of the prediction
    The files will in this case be prefixed with a number so they are ordered 
    in a way that is interesting for evaluation. If a mask was available, this
    prefix will be the % overlap of the mask and the prediction. If no mask is
    available, the prefix is the % white pixels in the prediction.
        
    Args
        input_image_dir: dir where the input images are located
        output_base_dir: dir where the output will be put
        border_pixels_to_ignore: because the segmentation at the borders of the
                                 input images images is not as good, you can
                                 specify that x pixels need to be ignored
        input_mask_dir: optional dir where the mask images are located
        batch_size: batch size to use while predicting. This must be 
                    choosen depending on the neural network architecture
                    and available memory on you GPU.
        evaluate_mode: True to run in evaluate mode
        projection: Normally the projection should be in the raster file. If it 
                    is not, you can explicitly specify one.
        force: False to skip images that already have a prediction, true to
               ignore existing predictions and overwrite them
    """
    
    # Check if input params are ok
    if not os.path.exists(input_image_dir):
        logger.warn(f"In predict_dir, but input_image_dir doesn't exist, so return: {input_image_dir}")
        return

    logger.info(f"Start predict for input_image_dir: {input_image_dir}")

    # If we are using evaluate mode, change the output dir...
    if evaluate_mode:
        output_base_dir = output_base_dir + '_eval'
        
    # Create the output dir's if they don't exist yet...
    for dir in [output_base_dir]:
        if not os.path.exists(dir):
            os.mkdir(dir)

    # Get list of all image files to process...
    image_filepaths = []
    input_ext = ['.tif', '.jpg']
    for input_ext_cur in input_ext:
        image_filepaths.extend(glob.glob(f"{input_image_dir}{os.sep}**{os.sep}*{input_ext_cur}", recursive=True))
    nb_todo = len(image_filepaths)
    logger.info(f"Found {nb_todo} {input_ext} images to predict on in {input_image_dir}")
    
    # If force is false, get list of all existing predictions
    # Getting the list once is way faster than checking file per file later on!
    images_done_log_filepath = os.path.join(output_base_dir, "images_done.txt")
    images_error_log_filepath = os.path.join(output_base_dir, "images_error.txt")
    if force is False:
        # First read the listing files if they exists
        image_done_filenames = set()
        if os.path.exists(images_done_log_filepath):
            with open(images_done_log_filepath) as f:
                for filename in f:
                    image_done_filenames.add(filename.rstrip())
        if os.path.exists(images_error_log_filepath):                    
            with open(images_error_log_filepath) as f:
                for filename in f:
                    image_done_filenames.add(filename.rstrip())
        if len(image_done_filenames) > 0:
            logger.info(f"Found {len(image_done_filenames)} predicted images in {images_done_log_filepath} and {images_error_log_filepath}, they will be skipped")

    # Loop through all files to process them...
    curr_batch_image_infos = []
    nb_processed = 0
    image_filepaths_sorted = sorted(image_filepaths)
    with futures.ThreadPoolExecutor(batch_size) as pool:
        
        for i, image_filepath in enumerate(image_filepaths_sorted):
    
            # If force is false and prediction exists... skip
            if force is False:
               filename = os.path.basename(image_filepath)
               if filename in image_done_filenames:
                   logger.debug(f"Predict for image has already been done before and force is False, so skip: {filename}")
                   continue
                    
            # Prepare the filepath for the output
            image_filepath_noext = os.path.splitext(image_filepath)[0]
            if evaluate_mode:
                # In evaluate mode, put everyting in output base dir for easier 
                # comparison
                _, image_filename_noext = os.path.split(image_filepath_noext)
                tmp_output_filepath = os.path.join(output_base_dir, image_filename_noext)
            else:
                tmp_output_filepath = image_filepath_noext.replace(input_image_dir, output_base_dir)
            output_pred_filepath = f"{tmp_output_filepath}_pred.tif"       
            output_dir, _ = os.path.split(output_pred_filepath)
            
            # Init start time at the first file that isn't skipped
            if nb_processed == 0:
                start_time = datetime.datetime.now()
            nb_processed += 1
            
            # Append the image info to the batch array so they can be treated in 
            # bulk if the batch size is reached
            logger.debug(f"Start predict for image {image_filepath}")                   
            curr_batch_image_infos.append({'input_image_filepath': image_filepath,
                                           'output_pred_filepath': output_pred_filepath,
                                           'output_dir': output_dir})
            
            # If the batch size is reached or we are at the last images
            nb_images_in_batch = len(curr_batch_image_infos)
            curr_batch_image_infos_ext = []
            if(nb_images_in_batch == batch_size or i == (nb_todo-1)):
                start_time_batch_read = datetime.datetime.now()
                
                # Read all input images for the batch (in parallel)
                logger.debug(f"Start reading input {nb_images_in_batch} images")
                
                # Put arguments to pass to map in lists
                read_arg_filepaths = [info.get('input_image_filepath') for info in curr_batch_image_infos]
                read_arg_projections = [projection_if_missing for info in curr_batch_image_infos]
                
                # Exec read in parallel and extract result
                read_results = pool.map(read_image,            # Function 
                                        read_arg_filepaths,    # Arg 1-list
                                        read_arg_projections)  # Arg 2-list
                for j, read_result in enumerate(read_results):
                    curr_batch_image_infos_ext.append(
                            {'input_image_filepath': curr_batch_image_infos[j]['input_image_filepath'],
                             'output_pred_filepath': curr_batch_image_infos[j]['output_pred_filepath'],
                             'output_dir': curr_batch_image_infos[j]['output_dir'],
                             'image_crs': read_result['image_crs'],
                             'image_transform': read_result['image_transform'],
                             'image_data': read_result['image_data']})

                # Predict!
                logger.debug(f"Start prediction for {nb_images_in_batch} images")
                images = [info.get('image_data') for info in curr_batch_image_infos_ext]
                curr_batch_image_pred_arr = model.predict_on_batch(
                        np.asarray(images))
                
                # Postprocess predictions
                # Remark: trying to parallelize this doesn't seem to help at all!
                logger.debug("Start post-processing")    
                for j, image_info in enumerate(curr_batch_image_infos_ext):    
                    try:
                        # Save prediction
                        image_pred_uint8_cleaned = clean_prediction(
                                image_pred_arr=curr_batch_image_pred_arr[j], 
                                border_pixels_to_ignore=border_pixels_to_ignore)
                        image_pred_filepath = save_prediction_uint8(
                                image_filepath=image_info['input_image_filepath'],
                                image_pred_uint8_cleaned=image_pred_uint8_cleaned,
                                image_crs=image_info['image_crs'],
                                image_transform=image_info['image_transform'],
                                output_dir=image_info['output_dir'],
                                force=force)

                        # Postprocess for evaluation
                        if evaluate_mode is True:
                            # Create binary version and postprocess
                            image_pred_uint8_cleaned_bin = postp.to_binary_uint8(image_pred_uint8_cleaned, 125)                       
                            postp.postprocess_for_evaluation(
                                    image_filepath=image_info['input_image_filepath'],
                                    image_crs=image_info['image_crs'],
                                    image_transform=image_info['image_transform'],
                                    image_pred_filepath=image_pred_filepath,
                                    image_pred_uint8_cleaned_bin=image_pred_uint8_cleaned_bin,
                                    output_dir=image_info['output_dir'],
                                    input_image_dir=input_image_dir,
                                    input_mask_dir=input_mask_dir,
                                    border_pixels_to_ignore=border_pixels_to_ignore,
                                    force=force)
                                
                        # Write line to file with done files...
                        with open(images_done_log_filepath, "a+") as f:
                            f.write(os.path.basename(
                                    image_info['input_image_filepath']) + '\n')
                    except:
                        logger.exception(f"Error postprocessing pred for {image_info['input_image_filepath']}")
    
                        # Write line to file with done files...
                        with open(images_error_log_filepath, "a+") as f:
                            f.write(os.path.basename(
                                    image_info['input_image_filepath']) + '\n')
                logger.debug("Post-processing ready")

                # Log the progress and prediction speed
                time_passed_s = (datetime.datetime.now()-start_time).total_seconds()
                time_passed_lastbatch_s = (datetime.datetime.now()-start_time_batch_read).total_seconds()
                if time_passed_s > 0 and time_passed_lastbatch_s > 0:
                    nb_per_hour = (nb_processed/time_passed_s) * 3600
                    nb_per_hour_lastbatch = (nb_images_in_batch/time_passed_lastbatch_s) * 3600
                    hours_to_go = (int)((nb_todo - i)/nb_per_hour)
                    min_to_go = (int)((((nb_todo - i)/nb_per_hour)%1)*60)
                    print(f"\r{hours_to_go}:{min_to_go} left for {nb_todo-i} todo at {nb_per_hour:0.0f}/h ({nb_per_hour_lastbatch:0.0f}/h last batch) in ...{input_image_dir[-30:]}",
                          end='', flush=True)
                
                # Reset variable for next batch
                curr_batch_image_infos = []

def read_image(image_filepath: str,
               projection_if_missing: str = None):

    # Read input file
    # Because sometimes a read seems to fail, retry up to 3 times...
    retry_count = 0
    while True:
        try:
            with rio.open(image_filepath) as image_ds:
                # Read geo info
                image_crs = image_ds.profile['crs']
                image_transform = image_ds.transform
                
                # Read pixelsn change from (channels, width, height) to 
                # (width, height, channels) and normalize to values between 0 and 1
                image_data = image_ds.read()
                image_data = rio_plot.reshape_as_image(image_data)
                image_data = image_data / 255.0 
            
            # Read worked, so jump out of the loop...
            break
        except Exception as ex:
            retry_count += 1
            logger.warning(f"Read failed, retry nb {retry_count} for {image_filepath}")
            if retry_count >= 3:
                message = f"STOP: Read failed {retry_count} times for {image_filepath}, with exception: {ex}"
                logger.error(message)
                raise Exception(message)
        
    # The read was successfull, now check if there was a projection in the 
    # file and/or if one was provided
    if image_crs is None:
        if projection_if_missing is not None:
            image_crs = rio.crs.CRS.from_string(projection_if_missing)
        else:
            message = f"Image doesn't contain projection and no projection_if_missing provided!: {image_filepath}"
            logger.error(message)
            raise Exception(message)
        
    # Now return the result
    result = {'image_data': image_data,
              'image_crs': image_crs,
              'image_transform': image_transform,
              'image_filepath': image_filepath}
    return result

def clean_prediction(
        image_pred_arr: np.array,
        border_pixels_to_ignore: int = 0) -> np.array:

    # Check the number of channels of the output prediction
    image_pred_shape = image_pred_arr.shape
    n_channels = image_pred_shape[2]
    if n_channels > 1:
        raise Exception(f"Not implemented: processing prediction output with multiple channels: {n_channels}")    

    # Input should be float32
    if image_pred_arr.dtype != np.float32:
        raise Exception(f"image prediction is of the wrong type: {image_pred_arr.dtype}") 
    
    # Convert to uint8
    image_pred_uint8 = (image_pred_arr * 10).astype(np.uint8)
    image_pred_uint8 = image_pred_uint8 * 25
    #image_pred_uint8 = (image_pred_arr * 255).astype(np.uint8)
    
    # Reshape array from 4 dims (image_id, width, height, nb_channels) to 2.
    image_pred_uint8 = image_pred_uint8.reshape((image_pred_shape[0], image_pred_shape[1]))

    # Make the pixels at the borders of the prediction black so they are ignored
    image_pred_uint8_cropped = image_pred_uint8
    if border_pixels_to_ignore and border_pixels_to_ignore > 0:
        image_pred_uint8_cropped[0:border_pixels_to_ignore,:] = 0    # Left border
        image_pred_uint8_cropped[-border_pixels_to_ignore:,:] = 0    # Right border
        image_pred_uint8_cropped[:,0:border_pixels_to_ignore] = 0    # Top border
        image_pred_uint8_cropped[:,-border_pixels_to_ignore:] = 0    # Bottom border
    return image_pred_uint8_cropped

def save_prediction_uint8(
        image_filepath: str,
        image_pred_uint8_cleaned,
        image_crs: str,
        image_transform,
        output_dir: str,
        border_pixels_to_ignore: int = None,
        force: bool = False) -> str:

    ##### Init #####
    # If no decent transform metadata, stop!
    if image_transform is None or image_transform[0] == 0:
        message = f"No transform found for {image_filepath}: {image_transform}"
        logger.error(message)
        raise Exception(message)

    # Make sure the output dir exists...
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    
    # Prepare the filepath for the output
    _, image_filename = os.path.split(image_filepath)
    image_filename_noext, _ = os.path.splitext(image_filename)
    output_filepath = f"{output_dir}{os.sep}{image_filename_noext}_pred.tif"
                
    # Check if the result is entirely black... if so, don't save
    thresshold = 125
    if not np.any(image_pred_uint8_cleaned >= thresshold):
        logger.debug('Prediction is entirely black!')
        return None
        
    # Write prediction to file
    logger.debug("Save +- original prediction")
    image_shape = image_pred_uint8_cleaned.shape
    image_width = image_shape[0]
    image_height = image_shape[1]
    with rio.open(output_filepath, 'w', driver='GTiff', tiled='no',
                  compress='lzw', predictor=2, num_threads=4,
                  height=image_height, width=image_width, 
                  count=1, dtype=rio.uint8, crs=image_crs, transform=image_transform) as dst:
        dst.write(image_pred_uint8_cleaned, 1)
    
    return output_filepath
        
# If the script is ran directly...
if __name__ == '__main__':
    raise Exception("Not implemented")
    