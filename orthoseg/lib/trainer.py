# -*- coding: utf-8 -*-
"""
Module with high-level operations to segment images.
"""

import logging
import math
import os
from pathlib import Path
from typing import Tuple, Optional

import numpy as np
import tensorflow as tf
from tensorflow import keras as kr
#import keras as kr

import pandas as pd
from PIL import Image

import orthoseg.model.model_factory as mf
import orthoseg.model.model_helper as mh

#-------------------------------------------------------------
# First define/init some general variables/constants
#-------------------------------------------------------------
# Get a logger...
logger = logging.getLogger(__name__)
#logger.setLevel(logging.DEBUG)

#-------------------------------------------------------------
# The real work
#-------------------------------------------------------------

def train(
        traindata_dir: Path,
        validationdata_dir: Path,
        model_save_dir: Path,
        segment_subject: str,
        traindata_id: int,
        hyperparams: mh.HyperParams, 
        model_preload_filepath: Optional[Path] = None,
        image_width: int = 512,
        image_height: int = 512,
        image_subdir: str = "image",
        mask_subdir: str = "mask",
        save_augmented_subdir: str = None):
    """
    Create a new or load an existing neural network and train it using 
    data from the train and validation directories specified.
    
    The best models will be saved to model_save_dir. The filenames of the 
    models will be constructed like this:
    {model_save_base_filename}_{combined_acc}_{train_acc}_{validation_acc}_{epoch}
        * combined_acc: average of train_acc and validation_acc
        * train_acc: the jaccard coëficient of train dataset for the model
        * validation_acc: the jaccard coëficient of validation dataset 
    In the scripts, if the "best model" is mentioned, this is the one with the 
    highest "combined_acc".
    
    Args
        traindata_dir: dir where the train data is located
        validationdata_dir: dir where the validation data is located
        model_save_dir: dir where (intermediate) best models will be saved
        segment_subject (str): segment subject 
        traindata_id (int): train data version
        hyperparams (mh.HyperParams): the hyper parameters to use for the model
        image_width: width the input images will be rescaled to for training
        image_height: height the input images will be rescaled to for training
        image_subdir: subdir where the images can be found in traindata_dir and validationdata_dir
        mask_subdir: subdir where the corresponding masks can be found in traindata_dir and validationdata_dir
        model_preload_filepath: filepath to the model to continue training on, 
                or None if you want to start from scratch
    """     
    ##### Init #####
    # These are the augmentations that will be applied to the input training images/masks
    # Remark: fill_mode + cval are defined as they are so missing pixels after eg. rotation
    #         are filled with 0, and so the mask will take care that they are +- ignored.

    # Create the train generator
    train_gen = create_train_generator(
            input_data_dir=traindata_dir,
            image_subdir=image_subdir, 
            mask_subdir=mask_subdir,
            image_augment_dict=hyperparams.train.image_augmentations, 
            mask_augment_dict=hyperparams.train.mask_augmentations, 
            batch_size=hyperparams.train.batch_size,
            target_size=(image_width, image_height), 
            nb_classes=len(hyperparams.architecture.classes), 
            save_to_subdir=save_augmented_subdir, 
            seed=2)

    # Create validation generator
    validation_augmentations = dict(rescale=hyperparams.train.image_augmentations['rescale'])
    validation_mask_augmentations = dict(rescale=hyperparams.train.mask_augmentations['rescale'])
    validation_gen = create_train_generator(
            input_data_dir=validationdata_dir,
            image_subdir=image_subdir, 
            mask_subdir=mask_subdir,
            image_augment_dict=validation_augmentations,
            mask_augment_dict=validation_mask_augmentations, 
            batch_size=hyperparams.train.batch_size,
            target_size=(image_width, image_height), 
            nb_classes=len(hyperparams.architecture.classes), 
            save_to_subdir=save_augmented_subdir,
            shuffle=False, 
            seed=3)

    # Get the max epoch number from the log file if it exists...
    start_epoch = 0
    model_save_base_filename = mh.format_model_basefilename(
            segment_subject=segment_subject,
            traindata_id=traindata_id,
            architecture_id=hyperparams.architecture.architecture_id,
            trainparams_id=hyperparams.train.trainparams_id)
    csv_log_filepath = model_save_dir / (model_save_base_filename + '_log.csv')
    if csv_log_filepath.exists() and os.path.getsize(csv_log_filepath) > 0:
        if not model_preload_filepath:
            message = f"STOP: log file exists but preload model file not specified!!!: {csv_log_filepath}"
            logger.critical(message)
            raise Exception(message)
        
        train_log_csv = pd.read_csv(csv_log_filepath, sep=';')
        logger.debug(f"train_log csv contents:\n{train_log_csv}")
        start_epoch = train_log_csv['epoch'].max()
        hyperparams.train.optimizer_params['learning_rate'] = train_log_csv['lr'].to_numeric().min()
    logger.info(f"start_epoch: {start_epoch}, learning_rate: {hyperparams.train.optimizer_params['learning_rate']}")
       
    # If no existing model provided, create it from scratch
    if not model_preload_filepath:
        # Get the model we want to use
        model = mf.get_model(
                architecture=hyperparams.architecture.architecture, 
                nb_channels=hyperparams.architecture.nb_channels, 
                nb_classes=len(hyperparams.architecture.classes), 
                activation=hyperparams.architecture.activation_function)
        
        # Save the model architecture to json
        model_json_filepath = model_save_dir / f"{model_save_base_filename}_model.json"
        if not model_save_dir.exists():
            model_save_dir.mkdir(parents=True)
        if not model_json_filepath.exists():
            with model_json_filepath.open('w') as dst:
                dst.write(str(model.to_json()))
    else:
        # If a preload model is provided, load that if it exists...
        if not model_preload_filepath.exists():
            message = f"Error: preload model file doesn't exist: {model_preload_filepath}"
            logger.critical(message)
            raise Exception(message)
        
        # Load the existing model
        # Remark: compiling during load crashes, so compile 'manually'
        logger.info(f"Load model from {model_preload_filepath}")
        model = mf.load_model(model_preload_filepath, compile=False)

    # Now prepare the model for training
    nb_gpu = len(tf.config.experimental.list_physical_devices('GPU'))

    # TODO: because of bug in tensorflow 1.14, multi GPU doesn't work (this way),
    # so always use standard model
    if nb_gpu <= 1:
        model_for_train = model
        logger.info(f"Train using single GPU or CPU, with nb_gpu: {nb_gpu}")
    else:
        # If multiple GPU's available, create multi_gpu_model
        try:
            model_for_train = kr.utils.multi_gpu_model(model, gpus=nb_gpu, cpu_relocation=True)
            logger.info(f"Train using multiple GPUs: {nb_gpu}, batch size becomes: {hyperparams.train.batch_size*nb_gpu}")
            hyperparams.train.batch_size *= nb_gpu
        except ValueError:
            logger.info("Train using single GPU or CPU")
            model_for_train = model

    model_for_train = mf.compile_model(
            model=model_for_train, 
            optimizer=hyperparams.train.optimizer, 
            optimizer_params=hyperparams.train.optimizer_params, 
            loss=hyperparams.train.loss_function, 
            class_weights=hyperparams.train.class_weights)

    # Define some callbacks for the training
    # Reduce the learning rate if the loss doesn't improve anymore
    reduce_lr = kr.callbacks.ReduceLROnPlateau(
            monitor='loss', factor=0.2, patience=20, min_lr=1e-20, verbose=True)

    # Custom callback that saves the best models using both train and 
    # validation metric
    # Remark: the save of the model should be done on the standard model, not
    #         on the parallel_model, otherwise issues to use it afterwards
    if nb_gpu > 1:
        model_template_for_save = model
    else:
        model_template_for_save = None
    model_checkpoint_saver = mh.ModelCheckpointExt(
            model_save_dir=model_save_dir, 
            segment_subject=segment_subject,
            traindata_id=traindata_id,
            architecture_id=hyperparams.architecture.architecture_id,
            trainparams_id=hyperparams.train.trainparams_id,
            monitor_metric=hyperparams.train.monitor_metric,
            monitor_metric_mode=hyperparams.train.monitor_metric_mode,
            save_format=hyperparams.train.save_format,
            save_best_only=hyperparams.train.save_best_only,
            save_min_accuracy=hyperparams.train.save_min_accuracy,
            model_template_for_save=model_template_for_save)

    # Callbacks for logging
    tensorboard_log_dir = model_save_dir / (model_save_base_filename + '_tensorboard_log')
    tensorboard_logger = kr.callbacks.TensorBoard(log_dir=str(tensorboard_log_dir))
    csv_logger = kr.callbacks.CSVLogger(str(csv_log_filepath), append=True, separator=';')

    # Stop if no more improvement
    early_stopping = kr.callbacks.EarlyStopping(
            monitor=hyperparams.train.earlystop_monitor_metric, 
            patience=hyperparams.train.earlystop_patience, restore_best_weights=False)
    
    # Prepare the parameters to pass to fit...
    # Supported filetypes to train/validate on
    input_ext = ['.tif', '.jpg', '.png']

    # Calculate the size of the input datasets
    #train_dataset_size = len(glob.glob(f"{traindata_dir}/{image_subdir}/*.*"))
    train_dataset_size = 0
    for input_ext_cur in input_ext:
        traindata_image_dir = traindata_dir / image_subdir
        train_dataset_size += len(list(traindata_image_dir.rglob('*' + input_ext_cur)))
    validation_dataset_size = 0    
    for input_ext_cur in input_ext:
        validationdata_image_dir =validationdata_dir / image_subdir
        validation_dataset_size += len(list(validationdata_image_dir.rglob('*' + input_ext_cur)))
    
    # Calculate the number of steps within an epoch
    # Remark: number of steps per epoch should be at least 1, even if nb samples < batch size...
    train_steps_per_epoch = math.ceil(train_dataset_size/hyperparams.train.batch_size)
    validation_steps_per_epoch = math.ceil(validation_dataset_size/hyperparams.train.batch_size)
    
    # Start training
    logger.info(f"Start training with batch_size: {hyperparams.train.batch_size}, train_dataset_size: {train_dataset_size}, train_steps_per_epoch: {train_steps_per_epoch}, validation_dataset_size: {validation_dataset_size}, validation_steps_per_epoch: {validation_steps_per_epoch}")
        
    logger.info(f"{hyperparams.toJSON()}")
    hyperparams_filepath = model_save_dir / f"{model_save_base_filename}_hyperparams.json"
    hyperparams_filepath.write_text(hyperparams.toJSON())

    try:
        # Eager seems to be 50% slower
        model_for_train.run_eagerly = False
        model_for_train.fit(
                train_gen, 
                steps_per_epoch=train_steps_per_epoch, 
                epochs=hyperparams.train.nb_epoch,
                validation_data=validation_gen,
                validation_steps=validation_steps_per_epoch,       # Number of items in validation/batch_size
                callbacks=[model_checkpoint_saver, 
                           reduce_lr, early_stopping,
                           tensorboard_logger,
                           csv_logger],
                initial_epoch=start_epoch)

        # Write some reporting
        train_report_path = model_save_dir / (model_save_base_filename + '_report.pdf')
        train_log_df = pd.read_csv(csv_log_filepath, sep=';')
        columns_to_keep = []
        for column in train_log_df.columns:
            if(column.endswith('accuracy')
            or column.endswith('f1-score')):
                columns_to_keep.append(column)

        train_log_vis_df = train_log_df[columns_to_keep]
        train_log_vis_df.plot().get_figure().savefig(train_report_path)

    finally:
        # Release the memory from the GPU...
        #from keras import backend as K
        #K.clear_session()
        kr.backend.clear_session()

def create_train_generator(
        input_data_dir: Path, 
        image_subdir: str, 
        mask_subdir: str,
        image_augment_dict: dict, 
        mask_augment_dict: dict, 
        batch_size: int = 32,
        image_color_mode: str = "rgb", 
        mask_color_mode: str = "grayscale",
        save_to_subdir: str = None, 
        image_save_prefix: str = 'image', 
        mask_save_prefix: str = 'mask',
        nb_classes: int = 1,
        target_size: Tuple[int, int] = (256,256), 
        shuffle: bool = True,
        seed: int = 1):
    """
    Creates a generator to generate and augment train images. The augmentations
    specified in aug_dict will be applied. For the augmentations that can be 
    specified in aug_dict look at the documentation of 
    keras.preprocessing.image.ImageDataGenerator
    
    For more info about the other parameters, check keras flow_from_directory.

    Remarks: * use the same seed for image_datagen and mask_datagen to ensure 
               the transformation for image and mask is the same
             * set save_to_dir = "your path" to check results of the generator
    """
    # Init
    # If there are more than two classes, the mask will have integers as values
    # to code the different masks in, and one hot-encoding will be applied to 
    # it, so it should not be rescaled!!!
    if nb_classes > 2:
        if(mask_augment_dict is not None 
           and 'rescale' in mask_augment_dict
           and mask_augment_dict['rescale'] != 1):
                raise Exception(f"With nb_classes > 2 ({nb_classes}), the mask should have a rescale value of 1, not {mask_augment_dict['rescale']}")

    # Create the image generators with the augment info
    image_datagen = kr.preprocessing.image.ImageDataGenerator(**image_augment_dict)
    mask_datagen = kr.preprocessing.image.ImageDataGenerator(**mask_augment_dict)

    # Format save_to_dir
    # Remark: flow_from_directory doesn't support Path, so supply str immediately as well,
    # otherwise, if str(Path) is used later on, it becomes 'None' instead of None !!!
    save_to_dir = None
    if save_to_subdir is not None:
        save_to_dir = input_data_dir / save_to_subdir
        if not save_to_dir.exists():
            save_to_dir.mkdir(parents=True)

    image_generator = image_datagen.flow_from_directory(
            directory=str(input_data_dir),
            classes=[image_subdir],
            class_mode=None,
            color_mode=image_color_mode,
            target_size=target_size,
            batch_size=batch_size,
            save_to_dir=None,
            save_prefix=image_save_prefix,
            shuffle=shuffle,
            seed=seed)

    mask_generator = mask_datagen.flow_from_directory(
            directory=str(input_data_dir),
            classes=[mask_subdir],
            class_mode=None,
            color_mode=mask_color_mode,
            target_size=target_size,
            batch_size=batch_size,
            save_to_dir=None,
            save_prefix=mask_save_prefix,
            shuffle=shuffle,
            seed=seed)

    train_generator = zip(image_generator, mask_generator)

    for batch_id, (image, mask) in enumerate(train_generator):
        # Cast to arrays to evade type errors
        image = np.array(image)
        mask = np.array(mask)

        # One-hot encode mask if multiple classes
        if nb_classes > 1:
            mask = kr.utils.to_categorical(mask, nb_classes)

        # Because the default save_to_dir option doesn't support saving the 
        # augmented masks in seperate files per class, implement this here.  
        if save_to_dir is not None:            
            # Save mask for every class seperately
            # Get the number of images in this batch + the nb classes
            mask_shape = mask.shape
            nb_images = mask_shape[0]
            nb_classes = mask_shape[3]

            # Loop through images in this batch
            for image_id in range(nb_images):
                # Slice the next image from the array
                image_to_save = image[image_id,:,:,:]
                
                # Reverse the rescale if there is one
                if(mask_augment_dict is not None 
                   and 'rescale' in mask_augment_dict
                   and mask_augment_dict['rescale'] != 1):
                    image_to_save = image_to_save / mask_augment_dict['rescale']

                # Now convert to uint8 image and save!
                im = Image.fromarray(image_to_save.astype(np.uint8), image_color_mode)
                image_path = save_to_dir / f"{batch_id}_{image_id}.jpg"
                im.save(image_path)

                # Loop through the masks for each class
                for channel_id in range(nb_classes):
                    mask_to_save = mask[image_id,:,:,channel_id]
                    mask_path = save_to_dir / f"{batch_id}_{image_id}_{channel_id}.png"
                    im = Image.fromarray((mask_to_save * 255).astype(np.uint8))
                    im.save(mask_path)
                    
        yield (image, mask)
    
# If the script is ran directly...
if __name__ == '__main__':
    raise Exception("Not implemented")
    