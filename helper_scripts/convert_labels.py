# -*- coding: utf-8 -*-
"""
Script to convert label files from old version to new version.
"""

import math
import os
from pathlib import Path
import sys

import geopandas as gpd
import pandas as pd
import shapely.geometry as sh_geom

from geofileops import geofile

# Because orthoseg isn't installed as package + it is higher in dir hierarchy, add root to sys.path
[sys.path.append(i) for i in ['.', '..']]
from orthoseg.helpers import config_helper as conf
from orthoseg.helpers import log_helper

def convert_traindata_v1tov2(
        labellocations_path: Path, 
        labeldata_path: Path,
        image_pixel_x_size: float,
        image_pixel_y_size: float,
        image_srs_width: float,
        image_srs_height: float):

    # Prepare relevant file paths
    label_dir = labellocations_path.parent
    labellocations_filename = labellocations_path.name
    subject = labellocations_filename.split('_')[0]
    label_train_path = label_dir / f"{subject}_trainlabels.shp"
    label_validation_path = label_dir / f"{subject}_validationlabels.shp"
    label_test_path = label_dir / f"{subject}_testlabels.shp"

    # If the trainlabels file doesn't exist in the old format, stop
    if not os.path.exists(label_train_path) or not os.path.exists(label_validation_path):
        raise Exception(f"Stop: input file(s) don't exist: {label_train_path} and/or {label_validation_path}")

    # Read train and validation labels
    label_train_gdf = geofile.read_file(label_train_path)
    label_train_gdf['traindata_type'] = 'train'
    if 'image' not in list(label_train_gdf.columns):
        label_train_gdf['image'] = None
    label_validation_gdf = geofile.read_file(label_validation_path)
    label_validation_gdf['traindata_type'] = 'validation'
    if 'image' not in list(label_validation_gdf.columns):
        label_validation_gdf['image'] = None
    label_gdf = gpd.GeoDataFrame(
            pd.concat([label_train_gdf, label_validation_gdf], ignore_index=True, sort=True), 
            crs=label_train_gdf.crs)

    # If test labels exist as well... read and add them as well
    if os.path.exists(label_test_path):
        label_test_gdf = geofile.read_file(label_test_path)
        label_test_gdf['traindata_type'] = 'test'
        if 'image' not in list(label_test_gdf.columns):
            label_test_gdf['image'] = None
        label_gdf = gpd.GeoDataFrame(
                pd.concat([label_gdf, label_test_gdf], ignore_index=True, sort=True), 
                crs=label_gdf.crs)

    # Now convert to the new format
    label_gdf['label_name'] = subject
    label_gdf.rename(columns={'desc': 'description', 'image': 'image_layer'}, inplace=True)

    labellocations_gdf = label_gdf[label_gdf['usebounds'] == 1]
    labellocations_gdf.drop(columns=['usebounds', 'burninmask'], inplace=True)

    def get_bbox_translated(geom):
        # The location geoms must be replaced by the relevant bbox 
        geom_bounds = geom.bounds        
        xmin = geom_bounds[0]-(geom_bounds[0]%image_pixel_x_size)-10
        ymin = geom_bounds[1]-(geom_bounds[1]%image_pixel_y_size)-10
        xmax = xmin + image_srs_width
        ymax = ymin + image_srs_height
        return sh_geom.box(xmin, ymin, xmax, ymax)

    labellocations_gdf['geometry'] = labellocations_gdf.geometry.apply(
            lambda geom: get_bbox_translated(geom))
    
    geofile.to_file(labellocations_gdf, labellocations_path)
    labeldata_gdf = label_gdf[label_gdf['burninmask'] == 1]
    labeldata_gdf.drop(columns=['usebounds', 'burninmask', 'traindata_type'], inplace=True)
    geofile.to_file(labeldata_gdf, labeldata_path)

if __name__ == "__main__":
    
    # Local script config
    segment_subject = 'waterbasins'

    # Prepare the path to the config dir,...
    script_dir = Path(os.path.abspath(__file__)).parent
    base_dir = script_dir.parent
    config_dir = base_dir / "config"
    layer_config_filepath = config_dir / 'image_layers.ini'

    # Read the config file
    config_filepaths = [config_dir / 'general.ini',
                        config_dir / f"{segment_subject}.ini",
                        config_dir / 'local_overrule.ini']
    conf.read_project_config(config_filepaths, layer_config_filepath)
    logger = log_helper.main_log_init(conf.dirs.getpath('log_dir'), __name__)      
    logger.info(f"Config used: \n{conf.pformat_config()}")

    # Init needed variables
    labels_dir = conf.dirs.getpath('labels_dir')
    labellocations_path = labels_dir / f"{segment_subject}_labellocations.gpkg"
    labeldata_path = labels_dir / f"{segment_subject}_labeldata.gpkg"
    image_pixel_x_size = conf.train.getfloat('image_pixel_x_size')
    image_pixel_y_size = conf.train.getfloat('image_pixel_y_size')
    image_pixel_width = conf.train.getint('image_pixel_width')
    image_pixel_height = conf.train.getint('image_pixel_height')
    image_srs_width = math.fabs(image_pixel_width*image_pixel_x_size)   # tile width in units of crs 
    image_srs_height = math.fabs(image_pixel_height*image_pixel_y_size) # tile height in units of crs
    
    # Check if input files in exist in v1, and if so, convert to v2
    convert_traindata_v1tov2(
            labellocations_path=labellocations_path,
            labeldata_path=labeldata_path,
            image_pixel_x_size=image_pixel_x_size,
            image_pixel_y_size=image_pixel_y_size,
            image_srs_width=image_srs_width,
            image_srs_height=image_srs_height)
        