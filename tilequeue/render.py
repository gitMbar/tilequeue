from contextlib import closing
from multiprocessing.pool import ThreadPool
from shapely.wkb import dumps
from shapely.wkb import loads
from tilequeue.format import json_format
from tilequeue.format import lookup_formatter
from tilequeue.format import mapbox_format
from tilequeue.format import topojson_format
from tilequeue.format import vtm_format
from TileStache.Geography import SphericalMercator
from TileStache.Goodies.VecTiles.ops import transform
from TileStache.Goodies.VecTiles.server import build_query
from TileStache.Goodies.VecTiles.server import get_features
from TileStache.Goodies.VecTiles.server import query_columns
from TileStache.Goodies.VecTiles.server import tolerances
import math


# This is what will get passed from fetching data. Ideally, this would
# be separate from the data, but at the moment the opensciencemap
# renderer mandates slightly different data. This forces us to track
# which data is which to ultimately dispatch on the correct formatter.
class RenderData(object):

    def __init__(self, format, feature_layers, bounds):
        self.format = format
        self.feature_layers = feature_layers
        self.bounds = bounds


# stores the sql columns needed per layer, zoom
column_name_cache = {}


def columns_for_query(conn_info, layer_name, zoom, bounds, query):
    srid = 900913
    key = (layer_name, zoom)
    columns = column_name_cache.get(key)
    if columns:
        return columns
    columns = query_columns(conn_info, srid, query, bounds)
    column_name_cache[key] = columns
    return columns


def find_columns_for_queries(conn_info, layer_data, zoom, bounds):
    columns_for_queries = []
    for layer_datum in layer_data:
        queries = layer_datum['queries']
        query = queries[min(zoom, len(queries) - 1)]
        if query is None:
            cols = None
        else:
            cols = columns_for_query(
                conn_info, layer_datum['name'], zoom, bounds, query)
        columns_for_queries.append(cols)
    return columns_for_queries


def build_feature_queries(bounds, layer_data, zoom, tolerance,
                          padding, scale, columns_for_queries):
    is_geo = False
    srid = 900913
    queries_to_execute = []
    for layer_datum, columns in zip(layer_data, columns_for_queries):
        queries = layer_datum['queries']
        subquery = queries[min(zoom, len(queries) - 1)]
        if subquery is None:
            query = None
        else:
            query = build_query(
                srid, subquery, columns, bounds, tolerance,
                is_geo, layer_datum['is_clipped'], padding, scale)
        queries_to_execute.append(
            (layer_datum['name'], layer_datum['geometry_types'], query))
    return queries_to_execute


def make_add_clipped_property(layer_data):
    def add_clipped_property(feature_layer):
        # mutates the feature_layer itself
        layer_name = feature_layer['name']
        for layer_datum in layer_data:
            if layer_datum['name'] == layer_name:
                if layer_datum['is_clipped']:
                    features = feature_layer['features']
                    for wkb, props, id in features:
                        props['clipped'] = True
                return feature_layer
        return feature_layer
    return add_clipped_property


class RenderDataFetcher(object):

    def __init__(self, conn_info, layer_data, formats,
                 update_feature_layer=None,
                 find_columns_for_queries=find_columns_for_queries):
        # TODO consider conn_info to instead be a function to call to retrieve
        # an actual postgis connection
        # we would need to duplicate the get_features function however
        self.conn_info = conn_info
        self.formats = formats
        self.layer_data = layer_data
        self.spherical_mercator = SphericalMercator()
        self.update_feature_layer = update_feature_layer
        self.find_columns_for_queries = find_columns_for_queries
        self.sql_thread_pool = None
        self._is_initialized = False

    def initialize(self):
        assert not self._is_initialized, 'Multiple initialization'

        # create a thread pool
        n_layers = 7
        # we execute vtm queries concurrently
        n_threads = n_layers * 2
        self.sql_thread_pool = ThreadPool(n_threads)

        self._is_initialized = True

    def __call__(self, coord):
        assert self._is_initialized, 'Need to call initialize first'

        ul = self.spherical_mercator.coordinateProj(coord)
        lr = self.spherical_mercator.coordinateProj(coord.down().right())
        bounds = (
            min(ul.x, lr.x),
            min(ul.y, lr.y),
            max(ul.x, lr.x),
            max(ul.y, lr.y)
        )

        zoom = coord.zoom
        tolerance = tolerances[zoom]
        non_vtm_padding = 0
        vtm_padding = 5 * tolerance
        # scaling for mapbox format will be performed in python
        non_vtm_scale = None
        vtm_scale = 4096

        has_vtm = any((format == vtm_format for format in self.formats))
        has_non_vtm = any((format != vtm_format for format in self.formats))

        # first determine the columns for the queries
        # we currently perform the actual query and ask for no data
        # we also cache this per layer, per zoom
        columns_for_queries = self.find_columns_for_queries(
            self.conn_info, self.layer_data, zoom, bounds)

        render_data = []

        if has_vtm:
            vtm_empty_results, vtm_async_results = enqueue_queries(
                self.sql_thread_pool, self.conn_info, self.layer_data, zoom,
                bounds, tolerance, vtm_padding, vtm_scale,
                columns_for_queries)

        if has_non_vtm:
            non_vtm_empty_results, non_vtm_async_results = enqueue_queries(
                self.sql_thread_pool, self.conn_info, self.layer_data, zoom,
                bounds, tolerance, non_vtm_padding, non_vtm_scale,
                columns_for_queries)

        def feature_layers_from_results(async_results):
            feature_layers = []
            for async_result in async_results:
                layer_name, features = async_result.get()
                feature_layer = dict(name=layer_name, features=features)
                if self.update_feature_layer:
                    feature_layer = self.update_feature_layer(feature_layer)
                feature_layers.append(feature_layer)
            return feature_layers

        if has_vtm:
            vtm_feature_layers = feature_layers_from_results(vtm_async_results)
            vtm_feature_layers.extend(vtm_empty_results)
            vtm_render_data = RenderData(
                vtm_format, vtm_feature_layers, bounds)
            render_data.append(vtm_render_data)

        if has_non_vtm:
            non_vtm_feature_layers = feature_layers_from_results(
                non_vtm_async_results)
            non_vtm_feature_layers.extend(non_vtm_empty_results)
            non_vtm_render_data = [
                RenderData(format, non_vtm_feature_layers, bounds)
                for format in self.formats if format != vtm_format
            ]
            render_data.extend(non_vtm_render_data)

        return render_data


def execute_query(conn_info, query, geometry_types, layer_name):
    features = get_features(conn_info, query, geometry_types)
    return layer_name, features


def enqueue_queries(thread_pool, conn_info, layer_data, zoom, bounds,
                    tolerance, padding, scale, columns):
    queries_to_execute = build_feature_queries(
        bounds, layer_data, zoom,
        tolerance, padding, scale, columns)

    empty_results = []
    async_results = []
    for layer_name, geometry_types, query in queries_to_execute:
        if query is None:
            empty_feature_layer = dict(name=layer_name, features=[])
            empty_results.append(empty_feature_layer)
        else:
            # TODO we'll want to do the geometry_types check outside
            # of the threads
            async_result = thread_pool.apply_async(
                execute_query,
                (conn_info, query, geometry_types, layer_name))
            async_results.append(async_result)

    return empty_results, async_results


half_circumference_meters = 20037508.342789244


def mercator_point_to_wgs84(point):
    x, y = point

    x /= half_circumference_meters
    y /= half_circumference_meters

    y = (2 * math.atan(math.exp(y * math.pi)) - (math.pi / 2)) / math.pi

    x *= 180
    y *= 180

    return x, y


def rescale_point(bounds, scale):
    minx, miny, maxx, maxy = bounds

    def fn(point):
        x, y = point
        x -= minx
        y -= miny
        x *= (scale / (maxx - minx))
        y *= (scale / (maxy - miny))
        return x, y

    return fn


def apply_to_all_coords(fn):
    return lambda shape: transform(shape, fn)


def transform_feature_layers(feature_layers, format, bounds, scale):
    if format in (json_format, topojson_format):
        transform_fn = apply_to_all_coords(mercator_point_to_wgs84)
    elif format == mapbox_format:
        transform_fn = apply_to_all_coords(rescale_point(bounds, scale))
    else:
        # because vtm gets its own query, it doesn't need any post processing
        return feature_layers

    transformed_feature_layers = []
    for feature_layer in feature_layers:
        features = feature_layer['features']
        transformed_features = []
        for wkb, props, id in features:
            shape = loads(wkb)
            new_shape = transform_fn(shape)
            new_wkb = dumps(new_shape)
            transformed_features.append((new_wkb, props, id))
        transformed_feature_layer = dict(
            name=feature_layer['name'],
            features=transformed_features,
        )
        transformed_feature_layers.append(transformed_feature_layer)

    return transformed_feature_layers


class RenderJob(object):

    scale = 4096

    def __init__(self, coord, formats, feature_fetcher, store,
                 lookup_formatter=lookup_formatter):
        self.coord = coord
        self.formats = formats
        self.feature_fetcher = feature_fetcher
        self.store = store

    def __call__(self):
        render_data = self.feature_fetcher(self.coord)
        for render_datum in render_data:
            format = render_datum.format
            feature_layers = render_datum.feature_layers
            bounds = render_datum.bounds

            feature_layers = transform_feature_layers(
                feature_layers, format, bounds, self.scale)

            formatter = lookup_formatter(format)
            with closing(self.store.output_fp(self.coord, format)) as store_fp:
                formatter(store_fp, feature_layers, self.coord, bounds)

    def __repr__(self):
        return 'RenderJob(%s, %s)' % (self.coord, self.format)


class RenderJobCreator(object):

    def __init__(self, tilestache_config, formats, store):
        self.tilestache_config = tilestache_config
        self.formats = formats
        self.feature_fetcher = make_feature_fetcher(tilestache_config, formats)
        self.store = store

    def initialize(self):
        # process local initialization
        self.feature_fetcher.initialize()

    def create(self, coord):
        return RenderJob(coord, self.formats, self.feature_fetcher,
                         self.store)

    def process_jobs_for_coord(self, coord):
        job = self.create(coord)
        job()


def make_feature_fetcher(tilestache_config, formats):
    # layer_data interface:
    # list of dicts with these keys: name, queries, is_clipped, geometry_types

    layers = tilestache_config.layers
    all_layer = layers.get('all')
    assert all_layer is not None, 'All layer is expected in tilestache config'
    layer_names = all_layer.provider.names
    layer_data = []
    conn_info = None
    for layer_name in layer_names:
        # NOTE: obtain postgis connection information from first layer
        # this assumes all connection info is exactly the same
        assert layer_name in layers, \
            ('Layer not found in config but found in all layers: %s'
             % layer_name)
        layer = layers[layer_name]
        if conn_info is None:
            conn_info = layer.provider.dbinfo
        layer_datum = dict(
            name=layer_name,
            queries=layer.provider.queries,
            is_clipped=layer.provider.clip,
            geometry_types=layer.provider.geometry_types,
        )
        layer_data.append(layer_datum)

    update_feature_layer = make_add_clipped_property(layer_data)
    data_fetcher = RenderDataFetcher(
        conn_info, layer_data, formats,
        update_feature_layer=update_feature_layer
    )
    return data_fetcher
