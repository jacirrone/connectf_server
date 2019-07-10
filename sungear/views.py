import json
import logging
import warnings
from collections import OrderedDict, defaultdict
from functools import reduce
from itertools import groupby
from operator import itemgetter, or_
from typing import DefaultDict, Dict, List, Tuple

import numpy as np
import pandas as pd
from django.core.cache import cache
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.views import View
from numpy.linalg import norm
from sklearn.metrics import pairwise_distances

from querytgdb.models import Analysis
from querytgdb.utils import PandasJSONEncoder, clear_data, get_metadata

logger = logging.getLogger(__name__)


class SungearException(ValueError):
    pass


class SungearNotFound(SungearException):
    pass


def get_unit_vector(v):
    return v / norm(v, axis=1)[:, None]


def get_vertices(n: int, r: float = 0.4375, center=(0.5, 0.5), start=0) -> np.ndarray:
    return r * np.array([
        [np.cos(i - start), np.sin(i - start)]
        for i in np.linspace(2 * np.pi, 0, n, endpoint=False)
    ]) + center


def scale_sizes(sizes, node_size):
    ptp = sizes.ptp()

    if ptp:
        return (sizes - sizes.min()) / ptp * 0.9 * node_size + 0.1 * node_size
    else:
        return np.repeat(node_size, sizes.shape)


NAN_COORD = np.array([np.nan, np.nan])
CENTER = np.array([0.5, 0.5])
RADIUS = 0.4375


def get_sungear(uid, filter_genes: List[str] = None) -> Tuple[Dict, bool]:
    df = cache.get(f'{uid}/tabular_output')
    if df is None:
        raise SungearNotFound()

    df = clear_data(df)

    if filter_genes:
        df = df.loc[df.index.isin(filter_genes), :].dropna(how='all', axis=1)

    df = pd.concat([df.iloc[:, ::2], df.iloc[:, 1::2]], axis=1)

    if df.shape[1] < 2:
        raise SungearException("Sungear needs at least 2 analyses.")

    analyses = Analysis.objects.filter(
        pk__in=df.columns.get_level_values(1)
    )

    metadata = get_metadata(analyses)

    gene_lists = OrderedDict(
        (name[1], set(col.index[col.notna()])) for name, col in df.iteritems()
    )

    total_genes = reduce(or_, gene_lists.values())
    gene_to_list: DefaultDict[str, List] = defaultdict(list)

    for name, genes in gene_lists.items():
        for g in (total_genes & genes):
            gene_to_list[g].append(name)

    num_lists = len(gene_lists)

    vertices = pd.DataFrame(get_vertices(num_lists, start=np.pi / 2))
    vertices.index = gene_lists.keys()

    intersects = []

    for name, group in groupby(
            sorted(((sorted(v), k) for k, v in gene_to_list.items()),
                   key=lambda x: (len(x[0]), x[0], x[1])),
            key=itemgetter(0)):
        intersects.append([name, vertices.loc[name, :].mean(axis=0),
                           tuple(map(itemgetter(1), group))])

    intersects = sorted(intersects, key=lambda n: (len(n[2]), len(n[1])))
    # drawing below

    if vertices.shape[0] == 2:
        inner_radius = RADIUS
        node_size = min(0.05, RADIUS * 0.2)
    else:
        inner_radius = RADIUS * np.cos(np.pi / num_lists)
        node_size = min(0.05, 2 * RADIUS * np.sin(np.pi / num_lists) / 3)

    polygon_area = 2 * np.pi * inner_radius ** 2

    sizes = np.array(list(map(lambda x: len(x[2]), intersects)))
    sizes_scaled = scale_sizes(sizes, node_size)

    node_area = np.sum(2 * np.pi * (sizes_scaled + 0.02 * node_size) ** 2)

    while (node_area / polygon_area) > 0.2:  # packing density
        node_size -= 0.002

        sizes_scaled = scale_sizes(sizes, node_size)
        node_area = np.sum(2 * np.pi * (sizes_scaled + 0.02 * node_size) ** 2)

    coords = np.array(list(map(itemgetter(1), intersects)))

    def row_mean(x):
        if not x.any():
            return NAN_COORD
        return np.average(coords[x], axis=0, weights=sizes_scaled[x] ** 2)

    il = np.tril_indices(len(intersects), -1)

    can_cache = False

    i = 0.4 * inner_radius

    def clamp(n):
        return min(i, max(n, 0.1 * node_size))

    s = 0.1 * node_size

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        prev_adjs = coords.shape[0]

        for j in range(50):
            pdist = pairwise_distances(coords)
            move_il = pdist[il].flatten() < sizes_scaled[il[0]] + sizes_scaled[il[1]] + 0.02 * node_size

            move_locs = np.full(pdist.shape, False)
            move_locs[il[0][move_il], il[1][move_il]] = True

            move = np.apply_along_axis(row_mean, 0, move_locs).T

            v = move - coords
            to_move = ~np.isnan(v).any(axis=1)

            rand_move = np.isclose(norm(v, axis=1), 0, atol=node_size * 0.1)
            if rand_move.any():
                v[rand_move] = get_unit_vector(
                    np.random.normal(size=(np.sum(rand_move), 2))) * i

            move_vecs = v[to_move, :]

            if move_vecs.shape[0] >= prev_adjs:
                i = clamp(i + s)
            else:
                i = clamp(i - s)
            prev_adjs = move_vecs.shape[0]

            # actually move
            move_vecs = get_unit_vector(move_vecs) * i
            coords[to_move, :] += move_vecs
            # move closer to center
            center_dist = norm(coords - CENTER, axis=1) + sizes_scaled - inner_radius
            far_coords = center_dist > 0

            center_move_dist = center_dist[far_coords, None] + node_size * 0.1
            center_move_dist += np.random.random(center_move_dist.shape) * node_size * 0.1

            coords[far_coords] += get_unit_vector(
                CENTER - coords[far_coords]) * center_move_dist  # add some randomness to center move
            logger.info(f"iteration: {j + 1}, {move_vecs.shape[0]} adjustments")

            if not move_vecs.shape[0]:
                can_cache = not filter_genes
                break

    for n, c, s in zip(intersects, coords, sizes_scaled):
        n[1] = c
        n.append(s)
        n.append(c + get_unit_vector((vertices.loc[n[0], :] - c).to_numpy()) * (1.2 * s + 0.1 * node_size))

    return {
               'vertices': [(idx, row) for idx, *row in vertices.itertuples(name=None)],
               'intersects': intersects,
               'metadata': metadata.to_dict('index')
           }, can_cache


# Create your views here.
class SungearView(View):
    def get(self, request, request_id):
        try:
            res = cache.get(f'{request_id}/sungear')
            if res is None:
                res, can_cache = get_sungear(request_id)
                if can_cache:
                    cache.set(f'{request_id}/sungear', res)
            return JsonResponse(res, encoder=PandasJSONEncoder)
        except SungearNotFound:
            raise Http404
        except SungearException as e:
            return HttpResponseBadRequest(e)

    def post(self, request, request_id):
        try:
            try:
                genes = json.loads(request.body)['genes']
                res, can_cache = get_sungear(request_id, genes)
            except (json.JSONDecodeError, KeyError):
                res = cache.get(f'{request_id}/sungear')
                if res is None:
                    res, can_cache = get_sungear(request_id)
                    if can_cache:
                        cache.set(f'{request_id}/sungear', res)
            return JsonResponse(res, encoder=PandasJSONEncoder)
        except SungearNotFound:
            raise Http404
        except SungearException as e:
            return HttpResponseBadRequest(e)
