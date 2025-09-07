import os
import geopandas as gpd
import glob
import fiona
import time

start_time = time.time()
def timer_decorator(start_time):
    def decorator(func):
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            print(f"Time elapsed: {elapsed:.4f}s")
            return result
        return wrapper
    return decorator

@timer_decorator(start_time)
def get_all_gdf_data(filepath: str) -> gpd.geodataframe.GeoDataFrame:
    if filepath.endswith('.pbf'):
        return gpd.read_file(filename=filepath, layer='lines', columns=["osm_id", "name", "highway", "other_tags", "geometry"])
    if filepath.endswith('.gdb'):
        return gpd. read_file(filename=filepath, layer='CENTERLINE', columns=["STREET_ENAME", "STREET_CNAME", "SHAPE_Length", "geometry"])

@timer_decorator(start_time)
def get_filepath_by_extension(extension: str) -> str:
    dataset_fp = os.path.join(os.getcwd(), 'dataset')
    for file in os.listdir(dataset_fp):
        if file.endswith(extension):
            return os.path.join(dataset_fp, file)

def main():
    gov_gdf_path = get_filepath_by_extension('.gdb')
    print('got gov_gdf_path')
    pbf_file_path = get_filepath_by_extension('.pbf')
    print('got pbf_file_path')

    pbf_gdf = get_all_gdf_data(pbf_file_path)
    gov_gdf = get_all_gdf_data(gov_gdf_path)

# 3.2440s
# Time elapsed: 3.4122s
if __name__ == "__main__":
    main()