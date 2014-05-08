# ---------------------------------------------------------------------------
# Purpose: transfer the landsat classied image into shielding multplier
# Input: loc, input raster file format: loc + '_terrain_class.img', dem
# Output: loc + '_ms' in each direction
# Created on 12 08 2011 by Tina Yang
# ---------------------------------------------------------------------------



    
# Import system & process modules
import sys, string, os, time, shutil, logging as log
import numexpr
from scipy import ndimage, signal
from os.path import join as pjoin
import numpy as np
from osgeo.gdalconst import *
from netCDF4 import Dataset
from vincenty import vinc_dist
from blrb import interpolate_grid
from osgeo import gdal, osr
import value_lookup
from _execute import execute


RADIANS_PER_DEGREE = 0.01745329251994329576923690768489

def shield(terrain, input_dem):    
    # start timing
    startTime = time.time()
    
#    # open the tile
#    temp_dataset = gdal.Open(input_dem) 
#    
#    # get image size, format, projection
#    cols = temp_dataset.RasterXSize
#    rows = temp_dataset.RasterYSize
#    bands = temp_dataset.RasterCount
#    log.info('Input raster format is %s' % temp_dataset.GetDriver().ShortName + '/ %s' % temp_dataset.GetDriver().LongName)
#    log.info('Image size is %s' % cols + 'x %s' % rows + 'x %s' % bands)
#    
#    # get georeference info
#    geotransform = temp_dataset.GetGeoTransform()
#    pixel_x_size = abs(geotransform[1]) 
#    pixel_y_size = abs(geotransform[5])
#    band = temp_dataset.GetRasterBand(1)     
#    elevation_array = band.ReadAsArray(0, 0, cols, rows)   

    print '\nDerive slope and reclassified aspect ...   '
    slope_array, aspect_array = get_slope_aspect(input_dem)
    
    print '\nReclassfy the terrain classes into initial shielding factors ...'
    ms_orig = terrain_class2ms_orig(terrain, input_dem)

    print '\nMoving average and combine the slope and aspect for each direction ...'
    convo_combine(ms_orig, slope_array, aspect_array)
    
    os.remove(ms_orig)
    del slope_array, aspect_array
    
    print '\nfinish successfully'

    # figure out how long the script took to run
    stopTime = time.time()
    sec = stopTime - startTime
    days = int(sec / 86400)
    sec -= 86400*days
    hrs = int(sec / 3600)
    sec -= 3600*hrs
    mins = int(sec / 60)
    sec -= 60*mins
    print 'The scipt took ', days, 'days, ', hrs, 'hours, ', mins, 'minutes ', '%.2f ' %sec, 'seconds'


class earth(object):

    # Mean radius
    RADIUS = 6371009.0  # (metres)

    # WGS-84
    #RADIUS = 6378135.0  # equatorial (metres)
    #RADIUS = 6356752.0  # polar (metres)

    # Length of Earth ellipsoid semi-major axis (metres)
    SEMI_MAJOR_AXIS = 6378137.0

    # WGS-84
    A = 6378137.0           # equatorial radius (metres)
    B = 6356752.3142        # polar radius (metres)
    F = (A - B) / A         # flattening
    ECC2 = 1.0 - B**2/A**2  # squared eccentricity

    MEAN_RADIUS = (A*2 + B) / 3

    # Earth ellipsoid eccentricity (dimensionless)
    #ECCENTRICITY = 0.00669438
    #ECC2 = math.pow(ECCENTRICITY, 2)

    # Earth rotational angular velocity (radians/sec)
    OMEGA = 0.000072722052


def get_pixel_size(dataset, (x, y)):
    """
    Returns X & Y sizes in metres of specified pixel as a tuple.
    N.B: Pixel ordinates are zero-based from top left
    """
    log.debug('(x, y) = (%f, %f)', x, y)
    
#    import pdb
#    pdb.set_trace()
    
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromWkt(dataset.GetProjection())
    
    geotransform = dataset.GetGeoTransform()
    log.debug('geotransform = %s', geotransform)
    
    latlong_spatial_reference = spatial_reference.CloneGeogCS()
    coord_transform_to_latlong = osr.CoordinateTransformation(spatial_reference, latlong_spatial_reference)

    # Determine pixel centre and edges in georeferenced coordinates
    xw = geotransform[0] + x * geotransform[1]
    yn = geotransform[3] + y * geotransform[5] 
    xc = geotransform[0] + (x + 0.5) * geotransform[1]
    yc = geotransform[3] + (y + 0.5) * geotransform[5] 
    xe = geotransform[0] + (x + 1.0) * geotransform[1]
    ys = geotransform[3] + (y + 1.0) * geotransform[5] 
    
    log.debug('xw = %f, yn = %f, xc = %f, yc = %f, xe = %f, ys = %f', xw, yn, xc, yc, xe, ys)
    
    # Convert georeferenced coordinates to lat/lon for Vincenty
    lon1, lat1, _z = coord_transform_to_latlong.TransformPoint(xw, yc, 0)
    lon2, lat2, _z = coord_transform_to_latlong.TransformPoint(xe, yc, 0)
    log.debug('For X size: (lon1, lat1) = (%f, %f), (lon2, lat2) = (%f, %f)', lon1, lat1, lon2, lat2)
    x_size, _az_to, _az_from = vinc_dist(earth.F, earth.A, 
                                         lat1 * RADIANS_PER_DEGREE, lon1 * RADIANS_PER_DEGREE, 
                                         lat2 * RADIANS_PER_DEGREE, lon2 * RADIANS_PER_DEGREE)
    
    lon1, lat1, _z = coord_transform_to_latlong.TransformPoint(xc, yn, 0)
    lon2, lat2, _z = coord_transform_to_latlong.TransformPoint(xc, ys, 0)
    log.debug('For Y size: (lon1, lat1) = (%f, %f), (lon2, lat2) = (%f, %f)', lon1, lat1, lon2, lat2)
    y_size, _az_to, _az_from = vinc_dist(earth.F, earth.A, 
                                         lat1 * RADIANS_PER_DEGREE, lon1 * RADIANS_PER_DEGREE, 
                                         lat2 * RADIANS_PER_DEGREE, lon2 * RADIANS_PER_DEGREE)
    
    log.debug('(x_size, y_size) = (%f, %f)', x_size, y_size)
    return (x_size, y_size)


def get_pixel_size_grids(dataset):
    """ Returns two grids with interpolated X and Y pixel sizes for given datasets"""
    
#    import pdb
#    pdb.set_trace()
    
    def get_pixel_x_size(x, y):
        #return get_pixel_size(dataset, (x, y))[0]
        return get_pixel_size(dataset, (x, y))[1]
    
    def get_pixel_y_size(x, y):
        #return get_pixel_size(dataset, (x, y))[1]
        return get_pixel_size(dataset, (x, y))[0]
    
    #x_size_grid = np.zeros((dataset.RasterXSize, dataset.RasterYSize)).astype(np.float32)
    x_size_grid = np.zeros((dataset.RasterYSize, dataset.RasterXSize)).astype(np.float32)
    interpolate_grid(depth=1, shape=x_size_grid.shape, eval_func=get_pixel_x_size, grid=x_size_grid)

    #y_size_grid = np.zeros((dataset.RasterXSize, dataset.RasterYSize)).astype(np.float32)
    y_size_grid = np.zeros((dataset.RasterYSize, dataset.RasterXSize)).astype(np.float32)
    interpolate_grid(depth=1, shape=y_size_grid.shape, eval_func=get_pixel_y_size, grid=y_size_grid)
    
    return (x_size_grid, y_size_grid)


def reclassify_aspect(data):
    """
    Purpose: transfer the landsat classied image into original terrain multplier
    Input: loc, input raster file format: loc + '_terrain_class.img', cyclone_area -yes or -no
    Output: loc + '_mz_orig'
    """
        
    outdata = np.zeros_like(data, dtype = np.int)
    outdata.fill(9)    
       
    outdata[np.where((data >= 0 ) & (data < 22.5))] = 1    
    outdata[np.where((337.5 <= data) & (data <= 360))] = 1
    
    for i in range(2, 9):
        outdata[np.where((22.5 + (i-2)*45 <= data) & (data < 67.5 + (i-2)*45))] = i    
   
    return outdata
    

def get_slope_aspect(input_dem):
    
    ds = gdal.Open(input_dem)    
    cols = ds.RasterXSize
    rows = ds.RasterYSize
    geotransform = ds.GetGeoTransform()
    pixel_x_size = abs(geotransform[1]) 
    pixel_y_size = abs(geotransform[5])
    band = ds.GetRasterBand(1)     
    elevation_array = band.ReadAsArray(0, 0, cols, rows)   
   
    x_m_array, y_m_array = get_pixel_size_grids(ds)
    
#    import pdb
#    pdb.set_trace()
    
    dzdx_array = ndimage.sobel(elevation_array, axis=1)/(8. * pixel_x_size)
    dzdx_array = numexpr.evaluate("dzdx_array * pixel_x_size / x_m_array")
    del x_m_array
    
    dzdy_array = ndimage.sobel(elevation_array, axis=0)/(8. * pixel_y_size)
    dzdy_array = numexpr.evaluate("dzdy_array * pixel_y_size / y_m_array")
    del y_m_array

    # find output folder
    ms_folder = pjoin(os.path.dirname(input_dem), 'shielding')
    file_name = os.path.basename(input_dem) 
    
    # Slope    
    hypotenuse_array = np.hypot(dzdx_array, dzdy_array)
    slope_array = numexpr.evaluate("arctan(hypotenuse_array) / RADIANS_PER_DEGREE")
    del hypotenuse_array          
    
    # Aspect
    # Convert angles from conventional radians to compass heading 0-360
    aspect_array = numexpr.evaluate("(450 - arctan2(dzdy_array, -dzdx_array) / RADIANS_PER_DEGREE) % 360")
    # Derive reclassifed aspect...    
    aspect_array_reclassify = reclassify_aspect(aspect_array)
    del aspect_array
        
#    # output format as ERDAS Imagine
#    driver = gdal.GetDriverByName('HFA')
#    
#    output_slope = pjoin(ms_folder, os.path.splitext(file_name)[0] + '_slope.img')    
#    output_slope_ds = driver.Create(output_slope, cols, rows, 1, GDT_Float32)
#    
#    # georeference the image and set the projection
#    output_geotransform = ds.GetGeoTransform()                                 
#    output_slope_ds.SetGeoTransform(output_geotransform)
#    output_slope_ds.SetProjection(ds.GetProjection()) 
#    
#    outBand_slope = output_slope_ds.GetRasterBand(1)
#    outBand_slope.WriteArray(slope_array)
#    del slope_array
#    
#    # flush data to disk, set the NoData value and calculate stats
#    outBand_slope.FlushCache()
#    outBand_slope.SetNoDataValue(-99)
#    outBand_slope.GetStatistics(0,1)
#    
#    # build pyramids
#    gdal.SetConfigOption('HFA_USE_RRD', 'YES')
#    output_slope_ds.BuildOverviews(overviewlist=[2,4,8,16,32,64,128])
#
#    #output aspect
#    output_aspect = pjoin(ms_folder, os.path.splitext(file_name)[0] + '_aspect.img')
#    output_aspect_ds = driver.Create(output_aspect, cols, rows, 1, GDT_Float32)
#    
#    # georeference the image and set the projection
#    output_geotransform = ds.GetGeoTransform()                                 
#    output_aspect_ds.SetGeoTransform(output_geotransform)
#    output_aspect_ds.SetProjection(ds.GetProjection())
#    
#    outBand_aspect = output_aspect_ds.GetRasterBand(1)
#    outBand_aspect.WriteArray(aspect_array_reclassify, 0, 0)
#    del aspect_array_reclassify
#    
#    # flush data to disk, set the NoData value and calculate stats
#    outBand_aspect.FlushCache()
#    outBand_aspect.SetNoDataValue(-99)
#    outBand_aspect.GetStatistics(0,1)
#
#    # build pyramids
#    gdal.SetConfigOption('HFA_USE_RRD', 'YES')
#    output_aspect_ds.BuildOverviews(overviewlist=[2,4,8,16,32,64,128])

    ds = None
    
    return slope_array, aspect_array_reclassify
   
def terrain_class2ms_orig(terrain, dem):  
    
#    import pdb
#    pdb.set_trace()

    ms_folder = pjoin(os.path.dirname(terrain), 'shielding')
    file_name = os.path.basename(terrain)
    # output format as ERDAS Imagine
    driver = gdal.GetDriverByName('HFA')
    
    # open the tile
    terrain_resample_ds = gdal.Open(terrain)
    #dem_ds = gdal.Open(dem) 
    
#    #resmaple the terrain as DEM
#    terrain_resample = pjoin(ms_folder, 'terrain_resample.img')
#    terrain_resample_ds = driver.Create(terrain_resample, dem_ds.RasterXSize, dem_ds.RasterYSize, 1, GDT_Int32)
#    terrain_resample_ds.SetGeoTransform(dem_ds.GetGeoTransform())
#    terrain_resample_ds.SetProjection(dem_ds.GetProjection())     
#    
#    gdal.ReprojectImage(terrain_ds, terrain_resample_ds, terrain_ds.GetProjection(), dem_ds.GetProjection(), GRA_Bilinear)
#    
    # get image size, format, projection
    cols = terrain_resample_ds.RasterXSize
    rows = terrain_resample_ds.RasterYSize
    
    # get georeference info    
    band = terrain_resample_ds.GetRasterBand(1)     
    data = band.ReadAsArray(0, 0, cols, rows)
    
    ms_init = value_lookup.ms_init 
    
    outdata = np.ones_like(data)
                
    for i in [1, 3, 4, 5]:
        outdata[data == i] = ms_init[i]/100.0
      
    ms_orig = pjoin(ms_folder, os.path.splitext(file_name)[0] + '_ms.img')    
    ms_orig_ds = driver.Create(ms_orig, terrain_resample_ds.RasterXSize, terrain_resample_ds.RasterYSize, 1, GDT_Float32)
#    #ms_orig_ds = driver.Create(ms_orig, terrain_ds.RasterXSize, terrain_ds.RasterYSize, 1, GDT_Float32)
    ms_orig_ds.SetGeoTransform(terrain_resample_ds.GetGeoTransform())
    ms_orig_ds.SetProjection(terrain_resample_ds.GetProjection()) 
    
    outBand_ms_orig = ms_orig_ds.GetRasterBand(1)
    outBand_ms_orig.WriteArray(outdata)
    del outdata
    
    # flush data to disk, set the NoData value and calculate stats
    outBand_ms_orig.FlushCache()
    outBand_ms_orig.SetNoDataValue(-99)
    outBand_ms_orig.GetStatistics(0,1)
    
    terrain_resample_ds = None
    
    return ms_orig
    
    


def convo_combine(ms_orig, slope_array, aspect_array):
    """
    apply convolution to the orginal shielding factor for each direction
    input: ms_orig.img
    outputs: shield_east.img, shield_west.img, ..., shield_sw.img etc.
    """  
    
#    import pdb
#    pdb.set_trace()
    
    ms_orig_ds = gdal.Open(ms_orig)    
    
    # get image size, format, projection
    cols = ms_orig_ds.RasterXSize
    rows = ms_orig_ds.RasterYSize
    
    # get georeference info    
    band = ms_orig_ds.GetRasterBand(1)     
    data = band.ReadAsArray(0, 0, cols, rows)
   
    if ms_orig_ds is None:
        print 'Could not open ' + ms_orig
        sys.exit(1)
    
    x_m_array, y_m_array = get_pixel_size_grids(ms_orig_ds)
    pixelWidth = 0.5 * (np.mean(x_m_array) + np.mean(y_m_array))
    
    print pixelWidth
    
    ms_folder = os.path.dirname(ms_orig)
    raster_folder = pjoin(ms_folder, 'raster')
    file_name = os.path.basename(ms_orig)            
        
    dire = ['w', 'e', 'n', 's', 'nw', 'ne', 'se', 'sw']

    for one_dir in dire:

        print one_dir
        output_dir = pjoin(raster_folder, os.path.splitext(file_name)[0] +  '_' + one_dir + '.img')

        if one_dir in ['w', 'e', 'n', 's']:
            kernel_size = int(100.0/pixelWidth) - 1
        else:
            kernel_size = int(100.0/pixelWidth)
            
        print 'convolution kernel size ' + str(kernel_size) 
        
        # if the resolution size is bigger than 100 m, no covolution just copy the initial shielding factor to each direction
        if kernel_size > 0: 
            outdata = np.zeros((rows, cols), np.float32)
    
            kern_dir = globals()['kern_' + one_dir]
            mask = kern_dir(kernel_size)
            outdata = blur_image(data, mask) 
        else:             
            outdata = data            
            
        result = combine(outdata, slope_array, aspect_array, one_dir) 
        del outdata 
        
        # output format as ERDAS Imagine
        driver = gdal.GetDriverByName('HFA')
        ms_dir_ds = driver.Create(output_dir, ms_orig_ds.RasterXSize, ms_orig_ds.RasterYSize, 1, GDT_Float32)
        
        # georeference the image and set the projection                           
        ms_dir_ds.SetGeoTransform(ms_orig_ds.GetGeoTransform())
        ms_dir_ds.SetProjection(ms_orig_ds.GetProjection()) 
        
        outBand_ms_dir = ms_dir_ds.GetRasterBand(1)
        outBand_ms_dir.WriteArray(result)
        del result
        
        # flush data to disk, set the NoData value and calculate stats
        outBand_ms_dir.FlushCache()
        outBand_ms_dir.SetNoDataValue(-99)
        #outBand_ms_dir.ComputeStatistics(1)
        outBand_ms_dir.GetStatistics(0,1) 
        
        ms_dir_ds = None

    ms_orig_ds = None
   


def combine(ms_orig_array, slope_array, aspect_array, one_dir):
    """
    To Generate Shielding Multiplier. This tool will be used for each direction to
    derive the shielding multipliers after moving averaging the shielding multiplier using direction_filter.py
    It also will remove the conservatism. 
    input: slope, reclassified aspect, shield origin in one direction
    output: shielding multiplier in the dirction
    """
    
#    import pdb
#    pdb.set_trace()
#    
#    slope_array = np.array([[0.2,0.4,4,5,13,11],
#                              [7,14,3,1,8,6],
#                              [1.8,9,4,7,2,18],
#                              [2,5,15,12,7,8],
#                              [3.6,8,3,1,5,2],
#                              [2.8,9.6,4,15,1,3]])
#    
#    aspect_array = np.array([[1,2,3,4,5,5],
#                              [7,5,3,1,8,6],
#                              [7,7,4,7,2,7],
#                              [2,5,7,7,7,8],
#                              [7,8,3,1,5,2],
#                              [7,7,4,7,1,3]])
#    
#    ms_orig_array = np.array([[0.2,0.4,0.4,0.5,0.98,0.85],
#                              [0.7,0.94,0.3,0.1,0.8,0.9],
#                              [0.8,0.9,0.4,0.77,0.2,0.98],
#                              [0.2,0.5,0.85,0.2,0.7,0.8],
#                              [0.6,0.8,0.9,0.89,0.5,0.2],
#                              [0.8,0.96,0.4,0.85,0.9,0.3]])                          
    
    dire_aspect = value_lookup.dire_aspect
    aspect_value = dire_aspect[one_dir]
    
    conservatism = 0.9
    up_degree = 12.30
    low_degree = 3.27
    
    out_ms = ms_orig_array
    
    slope_uplimit = np.where((slope_array >= 12.30) & (aspect_array == aspect_value))
    slope_middle = np.where((slope_array > 3.27 ) & (slope_array < 12.30) & (aspect_array == aspect_value))
    
    out_ms[slope_uplimit] = 1.0
    out_ms[slope_middle]  = ((1.0 - ms_orig_array[slope_middle]) * (slope_array[slope_middle] - low_degree)/(up_degree - low_degree)) + ms_orig_array[slope_middle] 
    
    ms_smaller = np.where(out_ms < conservatism)
    ms_bigger = np.where(out_ms >= conservatism)
    out_ms[ms_smaller] = out_ms[ms_smaller] * conservatism
    out_ms[ms_bigger] = out_ms[ms_bigger]**2
    
    return out_ms
    

   
def init_kern_diag(size):
    """
    Returns a mean kernel for convolutions, with dimensions
    (2*size+1, 2*size+1), it is north east direction
    """
    kernel = np.zeros((2*size+1, 2*size+1))
    kernel[size, size] = 1.0
    kernel[size-3:size, size+1] = 1.0
    kernel[size-2:size, size+2] = 1.0
    kernel[size-1, size+3] = 1.0    
    
    return kernel / kernel.sum()


def init_kern(size):
    """
    Returns a mean kernel for convolutions, with dimensions
    (2*size+1, 2*size+1), it is south direction
    """       
    kernel = np.zeros((2*size+1, 2*size+1))
    kernel[size+2:, size-1:size+2] = 1.0
    kernel[size:size+2, size] = 1.0    
    
    return kernel / kernel.sum()


def kern_w(size):
##    print np.rot90(init_kern(size), 3)    
    return np.rot90(init_kern(size), 3)


def kern_e(size):
##    print np.rot90(init_kern(size), 1)    
    return np.rot90(init_kern(size), 1)    


def kern_n(size):
##    print np.rot90(init_kern(size), 2)    
    return np.rot90(init_kern(size), 2)


def kern_s(size):
##    print init_kern(size)    
    return init_kern(size)


def kern_ne(size):
##    print init_kern_diag(size)    
    return init_kern_diag(size)


def kern_nw(size):
##    print np.fliplr(init_kern_diag(size))    
    return np.fliplr(init_kern_diag(size))


def kern_sw(size):
##    print np.flipud(np.fliplr(init_kern_diag(size)))    
    return np.flipud(np.fliplr(init_kern_diag(size)))


def kern_se(size):
##    print np.flipud(init_kern_diag(size))    
    return np.flipud(init_kern_diag(size))


def blur_image(im, kernel, mode='same'):
    """
    Blurs the image by convolving with a kernel (e.g. mean or gaussian) of typical
    size n. The optional keyword argument ny allows for a different size in the
    y direction.
    """     
    improc = signal.convolve(im, kernel, mode=mode)
    return(improc)


if __name__ == '__main__':
    dem = '/nas/gemd/climate_change/CHARS/B_Wind/Projects/Multipliers/validation/output_work/test_dem.img'
    terrain = '/nas/gemd/climate_change/CHARS/B_Wind/Projects/Multipliers/validation/output_work/test_terrain.img'
    shield(terrain, dem)