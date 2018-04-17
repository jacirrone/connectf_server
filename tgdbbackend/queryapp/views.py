import os
import pickle
import re
import shutil
import tempfile
import time
from collections import OrderedDict
from functools import partial, reduce
from itertools import chain, groupby
from operator import or_
from threading import Lock
from typing import Dict, Tuple

import environ
import numpy as np
import pandas as pd
from django.core.files.storage import FileSystemStorage, SuspiciousFileOperation
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseNotFound, JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from scipy.stats import fisher_exact
from statsmodels.stats.multitest import fdrcorrection

from querytgdb.models import Analysis, Metadata
from querytgdb.utils import query_tgdb
from querytgdb.utils.clustering import read_pickled_targetdbout
from querytgdb.utils.cytoscape import create_cytoscape_data
from querytgdb.utils.excel import create_excel_zip
from .utils import PandasJSONEncoder

lock = Lock()

storage = FileSystemStorage('commongenelists/')

ROOT_DIR = environ.Path(
    __file__) - 3  # (tgdbbackend/config/settings/common.py - 3 = tgdbbackend/)
APPS_DIR = ROOT_DIR.path('tgdbbackend')
STATIC_DIR = APPS_DIR.path('static').path('queryBuilder')

ANNOTATED = pd.read_pickle(str(APPS_DIR.path('static').path('annotated.pickle.gz')))
ANNOTATED = ANNOTATED[ANNOTATED['p-value'] < 0.0001]

ANNOTATED_PROMO = ANNOTATED[(ANNOTATED['stop'] - ANNOTATED['start'] + ANNOTATED['dist']) < 0]
ANN_PROMO_DEDUP = ANNOTATED_PROMO.drop_duplicates('match_id')
PROMO_CLUSTER_SIZE = ANN_PROMO_DEDUP.groupby('#pattern name').size()

ANNOTATED_BODY = ANNOTATED[ANNOTATED['dist'] > 0]
ANN_BODY_DEDUP = ANNOTATED_BODY.drop_duplicates('match_id')
BODY_CLUSTER_SIZE = ANN_BODY_DEDUP.groupby('#pattern name').size()


def save_file(dest_path, f):
    path = os.path.join(dest_path, os.path.basename(f.name))
    destination = open(path, 'wb')
    for chunk in f.chunks():
        destination.write(chunk)
    destination.close()
    return path


def set_tf_query(tf_query, tf_file_paths):
    j = 0
    if tf_query is None:
        return
    for i, val in enumerate(tf_query):
        begin = val.find("{")
        if begin != -1:
            end = val.find("}")
            tf_query[i] = val[:begin] + tf_file_paths[j] + val[end + 1:]
            j += 1


def parse_key(key: str) -> Tuple[str, str]:
    keys = key.split('_', maxsplit=3)

    right, *r = "_".join(keys[3:]).rpartition("_")
    right = re.sub(r'(\d+)_(\d+)$', r'\1.\2', right)

    return "_".join(keys[:3]), right


@method_decorator(csrf_exempt, name='dispatch')
class HandleQueryView(View):
    def post(self, request, *args, **kwargs):
        request_id = request.POST['requestId']

        tf_query = request.POST['tfs'].split(" ") if request.POST['tfs'] else None
        edges = request.POST['edges'].split(" ") if request.POST['edges'] else None
        metadata = request.POST['metas'].split(" ") if request.POST['metas'] else None

        targetgenes_file_path = None
        dirpath = tempfile.mkdtemp()

        tf_file_paths = []

        if 'targetgenes' in request.POST:
            try:
                targetgenes_file_path = save_file(dirpath,
                                                  storage.open("{}.txt".format(request.POST['targetgenes']), "rb"))
            except (FileNotFoundError, SuspiciousFileOperation):
                pass

        if len(request.FILES):
            if "targetgenes" in request.FILES:
                targetgenes_file_path = save_file(dirpath, request.FILES["targetgenes"])
            if "file-0" in request.FILES:
                i = 0
                name = "file-{}".format(i)
                while name in request.FILES:
                    tf_file_paths.append(
                        save_file(dirpath, request.FILES[name]))
                    i += 1
                    name = "file-{}".format(i)

        set_tf_query(tf_query, tf_file_paths)

        output = STATIC_DIR.path(request_id)

        try:
            df, out_metadata_df = query_tgdb(tf_query, edges, metadata, targetgenes_file_path, str(output))

            num_cols = df.columns.get_level_values(2).isin(['Pvalue', 'Log2FC']) | df.columns.get_level_values(
                0).isin(['UserList', 'Target Count'])

            p_values = df.columns.get_level_values(2) == 'Pvalue'

            nums = df.iloc[3:, num_cols].apply(partial(pd.to_numeric, errors='coerce'))

            nums = nums.where(~np.isinf(nums), None)

            df.iloc[3:, num_cols] = nums

            df = df.where(pd.notnull(df), None)

            merged_cells = []

            for i, level in enumerate(df.columns.labels):
                if i == 2:
                    continue

                # don't merge before column 9
                index = 8
                for label, group in groupby(level[8:]):
                    size = sum(1 for _ in group)
                    merged_cells.append({'row': i, 'col': index, 'colspan': size, 'rowspan': 1})
                    if i == 1:
                        merged_cells.extend(
                            {'row': a, 'col': index, 'colspan': size, 'rowspan': 1} for a in range(3, 6))
                    index += size

            columns = []

            for (i, num), p in zip(enumerate(num_cols), p_values):
                opt = {}
                if num:
                    if p:
                        opt.update({'type': 'p_value'})
                    else:
                        opt.update({'type': 'numeric'})
                    if i >= 8:
                        opt.update({'renderer': 'renderNumber', 'validator': 'exponential'})
                else:
                    opt.update({'type': 'text'})
                    if i >= 8:
                        opt.update({'renderer': 'renderTarget'})

                columns.append(opt)

            res = [{
                'data': list(chain(zip(*df.columns), df.itertuples(index=False, name=None))),
                'mergeCells': merged_cells,
                'columns': columns
            }]

            out_metadata_df.reset_index(inplace=True)
            meta_columns = [{"id": column, "name": column, "field": column} for column in out_metadata_df.columns]

            res.append({'columns': meta_columns,
                        'data': out_metadata_df.to_json(orient='index')})
            shutil.rmtree(dirpath)

            return JsonResponse(res, safe=False, encoder=PandasJSONEncoder)
        except ValueError as e:
            raise Http404('Query not available') from e


class CytoscapeJSONView(View):
    def get(self, request, request_id, name):
        try:
            outdir = str(STATIC_DIR.path("{}_json".format(request_id)))
            if not os.path.isdir(outdir):
                outdir = create_cytoscape_data(str(STATIC_DIR.path("{}_pickle".format(request_id))))
            with open("{}/{}.json".format(outdir, name)) as f:
                return HttpResponse(f, content_type="application/json; charset=utf-8")
        except FileNotFoundError as e:
            raise Http404 from e


class ExcelDownloadView(View):
    def get(self, request, request_id):
        try:
            out_file = str(STATIC_DIR.path("{}.zip".format(request_id)))
            if not os.path.exists(out_file):
                out_file = create_excel_zip(str(STATIC_DIR.path("{}_pickle".format(request_id))))
            with open(out_file, 'rb') as f:
                response = HttpResponse(f, content_type='application/zip')
                response['Content-Disposition'] = 'attachment; filename="query.zip"'

                return response
        except FileNotFoundError as e:
            raise Http404 from e


class HeatMapPNGView(View):
    def get(self, request, request_id):
        try:
            response = HttpResponse(content_type='image/svg+xml')
            read_pickled_targetdbout(
                str(STATIC_DIR.path("{}_pickle".format(request_id))),
                save_file=False
            ).savefig(response)

            return response
        except (FileNotFoundError, ValueError) as e:
            return HttpResponseNotFound(content_type='image/svg+xml')


def motif_enrichment(res: Dict[str, pd.Series], alpha: float = 0.05, show_reject: bool = True) -> pd.DataFrame:
    def get_list_enrichment(gene_list, annotated, annotated_dedup, ann_cluster_size,
                            alpha: float = 0.05) -> Tuple[pd.Series, pd.Series]:
        list_cluster_dedup = annotated[annotated.index.isin(gene_list)].drop_duplicates('match_id')
        list_cluster_size = list_cluster_dedup.groupby('#pattern name').size()

        def cluster_fisher(row):
            return fisher_exact(
                [[row[0], row[1] - row[0]],
                 [list_cluster_dedup.shape[0] - row[0],
                  annotated_dedup.shape[0] - list_cluster_dedup.shape[0] - row[1] + row[0]]],
                alternative='greater')[1]

        p_values = pd.concat([list_cluster_size, ann_cluster_size],
                             axis=1).fillna(0).apply(cluster_fisher, axis=1).sort_values()
        reject, adj_p = fdrcorrection(p_values, alpha=alpha, is_sorted=True)

        str_index = p_values.index.astype(str)

        return pd.Series(adj_p, index=str_index), pd.Series(reject, index=str_index)

    promo_enrich, promo_reject = zip(*map(partial(get_list_enrichment,
                                                  alpha=alpha,
                                                  annotated=ANNOTATED_PROMO,
                                                  annotated_dedup=ANN_PROMO_DEDUP,
                                                  ann_cluster_size=PROMO_CLUSTER_SIZE), res.values()))

    body_enrich, body_reject = zip(*map(partial(get_list_enrichment,
                                                alpha=alpha,
                                                annotated=ANNOTATED_BODY,
                                                annotated_dedup=ANN_BODY_DEDUP,
                                                ann_cluster_size=BODY_CLUSTER_SIZE), res.values()))

    df = pd.concat(chain.from_iterable(zip(promo_enrich, body_enrich)), axis=1)
    columns = list(chain.from_iterable(zip(
        map(lambda c: c + '_promo', res.keys()),
        map(lambda c: c + '_body', res.keys()))))
    df.columns = columns

    if show_reject:
        rejects = reduce(or_, chain(promo_reject, body_reject))
        df = df[rejects]
    else:
        rejects = pd.concat(chain.from_iterable(zip(promo_reject, body_reject)), axis=1)
        rejects.columns = columns
        df[~rejects] = np.nan
        df.dropna(how='all', inplace=True)

    return df


class MotifEnrichmentView(View):
    def get(self, request, request_id):
        with lock:
            if not request_id:
                return JsonResponse({}, status=404)
            try:
                alpha = float(request.GET.get('alpha', 0.05))

                p = str(STATIC_DIR.path("{}_pickle/df_jsondata.pkl".format(request_id)))

                if not os.path.exists(p):
                    time.sleep(3)

                data = pd.read_pickle(p)
                res = OrderedDict((name, col.index[col.notnull()].unique()) for name, col in data.iteritems())

                parsed_keys = list(map(parse_key, res.keys()))
                meta_ids, analysis_ids = zip(*parsed_keys)
                analyses = Analysis.objects.filter(analysis_fullid__in=analysis_ids)
                metadata = Metadata.objects.filter(meta_fullid__in=meta_ids)

                meta_dicts = []

                for meta_id, analysis_id in parsed_keys:
                    data = OrderedDict()
                    try:
                        data.update(OrderedDict(
                            analyses.get(analysis_fullid=analysis_id).analysisiddata_set.values_list('analysis_type',
                                                                                                     'analysis_value')))
                    except Analysis.DoesNotExist:
                        pass
                    try:
                        data.update(
                            metadata.get(meta_fullid=meta_id).metaiddata_set.values_list('meta_type', 'meta_value'))
                    except Metadata.DoesNotExist:
                        pass
                    meta_dicts.append(data)

                try:
                    with open(str(STATIC_DIR.path("{}_pickle/target_lists.pkl".format(request_id))), 'rb') as f:
                        target_lists = pickle.load(f)
                        meta_dicts.extend([{}] * len(target_lists))
                        res.update(target_lists)
                except FileNotFoundError:
                    pass

                df = motif_enrichment(res, alpha=alpha, show_reject=False)
                df = df.where(pd.notnull(df), None)

                return JsonResponse({
                    'columns': OrderedDict(zip(res.keys(), meta_dicts)),
                    'result': list(df.itertuples(name=None))
                }, encoder=PandasJSONEncoder)
            except FileNotFoundError as e:
                return JsonResponse({}, status=404)
            except (ValueError, TypeError) as e:
                return JsonResponse({}, status=400)


class MotifEnrichmentHeatmapView(View):
    def get(self, request, request_id):
        with lock:
            import matplotlib
            matplotlib.use('SVG')
            import matplotlib.pyplot as plt
            import seaborn as sns

            if not request_id:
                return HttpResponseNotFound(content_type='image/svg+xml')
            try:
                alpha = float(request.GET.get('alpha', 0.05))

                p = str(STATIC_DIR.path("{}_pickle/df_jsondata.pkl".format(request_id)))

                if not os.path.exists(p):
                    time.sleep(3)

                data = pd.read_pickle(p)
                res = OrderedDict((name, col.index[col.notnull()].unique()) for name, col in data.iteritems())

                try:
                    with open(str(STATIC_DIR.path("{}_pickle/target_lists.pkl".format(request_id))), 'rb') as f:
                        target_lists = pickle.load(f)
                        res.update(target_lists)
                except FileNotFoundError:
                    pass

                df = motif_enrichment(res, alpha=alpha)
                df = -np.log10(df)
                df = df.loc[:, (df != 0).any(axis=0)]
                # make max 30 for overly small p-values
                df[df > 30] = 30

                heatmap = sns.clustermap(df, cmap="YlGnBu", metric='correlation', method='average')
                plt.setp(heatmap.ax_heatmap.yaxis.get_majorticklabels(), rotation=0)
                plt.setp(heatmap.ax_heatmap.xaxis.get_majorticklabels(), rotation=270)

                response = HttpResponse(content_type='image/svg+xml')
                heatmap.savefig(response)

                return response
            except FileNotFoundError:
                return HttpResponseNotFound(content_type='image/svg+xml')
            except (ValueError, TypeError):
                return HttpResponseBadRequest(content_type='image/svg+xml')
