import time, copy
import json
from elasticsearch import Elasticsearch, NotFoundError, RequestError
from elasticsearch import helpers
import logging
import itertools

from biothings.utils.common import iter_n, timesofar, ask

# setup ES logging
import logging
formatter = logging.Formatter("%(levelname)s:%(name)s:%(message)s")
es_logger = logging.getLogger('elasticsearch')
es_logger.setLevel(logging.WARNING)
ch = logging.StreamHandler()
ch.setFormatter(formatter)
es_logger.addHandler(ch)

es_tracer = logging.getLogger('elasticsearch.trace')
es_tracer.setLevel(logging.WARNING)
ch = logging.StreamHandler()
ch.setFormatter(formatter)
es_tracer.addHandler(ch)

def verify_ids(doc_iter, index, doc_type, step=100000, ):
    '''verify how many docs from input interator/list overlapping with existing docs.'''

    index = index
    doc_type = doc_type
    es = get_es()
    q = {'query': {'ids': {"values": []}}}
    total_cnt = 0
    found_cnt = 0
    out = []
    for doc_batch in iter_n(doc_iter, n=step):
        id_li = [doc['_id'] for doc in doc_batch]
        # id_li = [doc['_id'].replace('chr', '') for doc in doc_batch]
        q['query']['ids']['values'] = id_li
        xres = es.search(index=index, doc_type=doc_type, body=q, _source=False)
        found_cnt += xres['hits']['total']
        total_cnt += len(id_li)
        out.extend([x['_id'] for x in xres['hits']['hits']])
    return out


def get_es(es_host):
    es = Elasticsearch(es_host, timeout=120)
    return es


def wrapper(func):
    '''this wrapper allows passing index and doc_type from wrapped method.'''
    def outter_fn(*args, **kwargs):
        self = args[0]
        index = kwargs.pop('index', self._index)
        doc_type = kwargs.pop('doc_type', self._doc_type)
        self._index = index
        self._doc_type = doc_type
        return func(*args, **kwargs)
    outter_fn.__doc__ = func.__doc__
    return outter_fn


class IndexerException(Exception): pass

class ESIndexer():
    def __init__(self, index, doc_type, es_host, step=10000,
                 number_of_shards=10, number_of_replicas=0):
        self._es = get_es(es_host)
        self._index = index
        self._doc_type = doc_type
        self.number_of_shards = number_of_shards # set number_of_shards when create_index
        self.number_of_replicas = number_of_replicas # set number_of_replicas when create_index
        self.step = step  # the bulk size when doing bulk operation.
        self.s = None   # optionally, can specify number of records to skip,
                        # useful to continue indexing after an error.

    @wrapper
    def get_biothing(self, bid, **kwargs):
        return self._es.get(index=self._index, id=bid, doc_type=self._doc_type, **kwargs)

    @wrapper
    def exists(self, bid):
        """return True/False if a biothing id exists or not."""
        try:
            doc = self.get_biothing(bid, fields=None)
            return doc['found']
        except NotFoundError:
            return False

    @wrapper
    def mexists(self, bid_list):
        q = {
            "query": {
                "ids": {
                    "values": bid_list
                }
            }
        }
        res = self._es.search(index=self._index, doc_type=self._doc_type, body=q, fields=None, size=len(bid_list))
        id_set = set([doc['_id'] for doc in res['hits']['hits']])
        return [(bid, bid in id_set) for bid in bid_list]

    @wrapper
    def count(self, q=None, raw=False):
        _res = self._es.count(self._index, self._doc_type, q)
        return _res if raw else _res['count']

    @wrapper
    def count_src(self, src):
        if isinstance(src, str):
            src = [src]
        cnt_d = {}
        for _src in src:
            q = {
                "query": {
                    "constant_score": {
                        "filter": {
                            "exists": {"field": _src}
                        }
                    }
                }
            }
            cnt_d[_src] = self.count(q)
        return cnt_d

    @wrapper
    def create_index(self, mapping=None, extra_settings={}):
        if not self._es.indices.exists(self._index):
            body = {
                'settings': {
                    'number_of_shards': self.number_of_shards,
                    "number_of_replicas": self.number_of_replicas,
                }
            }
            body["settings"].update(extra_settings)
            if mapping:
                mapping = {"mappings": mapping}
                body.update(mapping)
            self._es.indices.create(index=self._index, body=body)

    @wrapper
    def exists_index(self):
        return self._es.indices.exists(self._index)

    def index(self, doc, id=None, action="index"):
        '''add a doc to the index. If id is not None, the existing doc will be
           updated.
        '''
        return self._es.index(self._index, self._doc_type, doc, id=id, params={"op_type":action})

    def index_bulk(self, docs, step=None, action='index'):
        index_name = self._index
        doc_type = self._doc_type
        step = step or self.step

        def _get_bulk(doc):
            # keep original doc
            ndoc = copy.copy(doc)
            ndoc.update({
                "_index": index_name,
                "_type": doc_type,
                "_op_type" : action,
            })
            return ndoc
        actions = (_get_bulk(doc) for doc in docs)
        return helpers.bulk(self._es, actions, chunk_size=step)

    def delete_doc(self, id):
        '''delete a doc from the index based on passed id.'''
        return self._es.delete(self._index, self._doc_type, id)

    def delete_docs(self, ids, step=None):
        '''delete a list of docs in bulk.'''
        index_name = self._index
        doc_type = self._doc_type
        step = step or self.step

        def _get_bulk(_id):
            doc = {
                '_op_type': 'delete',
                "_index": index_name,
                "_type": doc_type,
                "_id": _id
            }
            return doc
        actions = (_get_bulk(_id) for _id in ids)
        return helpers.bulk(self._es, actions, chunk_size=step, stats_only=True, raise_on_error=False)

    def delete_index(self):
        self._es.indices.delete(self._index)

    def update(self, id, extra_doc, upsert=True):
        '''update an existing doc with extra_doc.
           allow to set upsert=True, to insert new docs.
        '''
        body = {'doc': extra_doc}
        if upsert:
            body['doc_as_upsert'] = True
        return self._es.update(self._index, self._doc_type, id, body)

    def update_docs(self, partial_docs, upsert=True, step=None, **kwargs):
        '''update a list of partial_docs in bulk.
           allow to set upsert=True, to insert new docs.
        '''
        index_name = self._index
        doc_type = self._doc_type
        step = step or self.step

        def _get_bulk(doc):
            doc = {
                '_op_type': 'update',
                "_index": index_name,
                "_type": doc_type,
                "_id": doc['_id'],
                "doc": doc
            }
            if upsert:
                doc['doc_as_upsert'] = True
            return doc
        actions = (_get_bulk(doc) for doc in partial_docs)
        return helpers.bulk(self._es, actions, chunk_size=step, **kwargs)

    def get_mapping(self):
        """return the current index mapping"""
        m = self._es.indices.get_mapping(index=self._index, doc_type=self._doc_type)
        return m

    def update_mapping(self, m):
        assert list(m) == [self._doc_type]
        assert 'properties' in m[self._doc_type]
        return self._es.indices.put_mapping(index=self._index, doc_type=self._doc_type, body=m)

    def get_mapping_meta(self):
        """return the current _meta field."""
        m = self.get_mapping()
        m = m[self._index]['mappings'][self._doc_type]
        return {"_meta": m["_meta"]}

    def update_mapping_meta(self, meta):
        allowed_keys = set(['_meta', '_timestamp'])
        if isinstance(meta, dict) and len(set(meta) - allowed_keys) == 0:
            body = {self._doc_type: meta}
            return self._es.indices.put_mapping(
                    doc_type=self._doc_type,
                    body=body,
                    index=self._index
                )
        else:
            raise ValueError('Input "meta" should have and only have "_meta" field.')

    @wrapper
    def build_index(self, collection, verbose=True, query=None, bulk=True, update=False, allow_upsert=True):
        index_name = self._index
        # update some settings for bulk indexing
        body = {
            "index": {
                "refresh_interval": "-1",              # disable refresh temporarily
                "auto_expand_replicas": "0-all",
            }
        }
        res = self._es.indices.put_settings(body, index_name)
        try:
            cnt = self._build_index_sequential(collection, verbose, query=query, bulk=bulk, update=update, allow_upsert=True)
        finally:
            # restore some settings after bulk indexing is done.
            body = {
                "index": {
                    "refresh_interval": "1s"              # default settings
                }
            }
            self._es.indices.put_settings(body, index_name)

            try:
                res = self._es.indices.flush()
                res = self._es.indices.refresh()
            except:
                pass

            time.sleep(1)
            src_cnt = collection.count(query)
            es_cnt = self.count()
            if src_cnt != es_cnt:
                raise IndexerException("Total count of documents does not match [{}, should be {}]".format(es_cnt, src_cnt))
            
            return es_cnt

    def _build_index_sequential(self, collection, verbose=False, query=None, bulk=True, update=False, allow_upsert=True):

        def rate_control(cnt, t):
            delay = 0
            if t > 90:
                delay = 30
            elif t > 60:
                delay = 10
            if delay:
                time.sleep(delay)

        from biothings.utils.mongo import doc_feeder
        src_docs = doc_feeder(collection, step=self.step, s=self.s, batch_callback=rate_control, query=query)
        if bulk:
            if update:
                # input doc will update existing one
                # if allow_upsert, create new one if not exist
                res = self.update_docs(src_docs, upsert=allow_upsert)
            else:
                # input doc will overwrite existing one
                res = self.index_bulk(src_docs)
            if len(res[1]) > 0:
                raise IndexerException("Error: {} docs failed indexing.".format(len(res[1])))
            return res[0]

        else:
            cnt = 0
            for doc in src_docs:
                self.index(doc)
                cnt += 1
            return cnt

    @wrapper
    def optimize(self, max_num_segments=1):
        '''optimize the default index.'''
        params = {
            "wait_for_merge": False,
            "max_num_segments": max_num_segments,
        }
        return self._es.indices.forcemerge(index=self._index, params=params)

    def clean_field(self, field, dryrun=True, step=5000):
        '''remove a top-level field from ES index, if the field is the only field of the doc,
           remove the doc as well.
           step is the size of bulk update on ES
           try first with dryrun turned on, and then perform the actual updates with dryrun off.
        '''
        q = {
            "query": {
                "constant_score": {
                    "filter": {
                        "exists": {
                            "field": field
                        }
                    }
                }
            }
        }
        cnt_orphan_doc = 0
        cnt = 0
        _li = []
        for doc in self.doc_feeder(query=q):
            if set(doc) == set(['_id', field]):
                cnt_orphan_doc += 1
                # delete orphan doc
                _li.append({
                    "delete": {
                        "_index": self._index,
                        "_type": self._doc_type,
                        "_id": doc['_id']
                    }
                })
            else:
                # otherwise, just remove the field from the doc
                _li.append({
                    "update": {
                        "_index": self._index,
                        "_type": self._doc_type,
                        "_id": doc['_id']
                    }
                })
                # this script update requires "script.disable_dynamic: false" setting
                # in elasticsearch.yml
                _li.append({"script": 'ctx._source.remove("{}")'.format(field)})

            cnt += 1
            if len(_li) == step:
                if not dryrun:
                    self._es.bulk(body=_li)
                _li = []
        if _li:
            if not dryrun:
                self._es.bulk(body=_li)
        
        return {"total": cnt, "updated": cnt - cnt_orphan_doc, "deleted": cnt_orphan_doc} 

    @wrapper
    def doc_feeder_using_helper(self, step=None, verbose=True, query=None, scroll='10m', **kwargs):
        # verbose unimplemented
        step = step or self.step
        q = query if query else {'query': {'match_all': {}}}
        for rawdoc in helpers.scan(client=self._es, query=q, scroll=scroll, index=self._index,
                        doc_type=self._doc_type,  **kwargs): 
            if rawdoc.get('_source', False):
                doc = rawdoc['_source']
                doc["_id"] = rawdoc["_id"]
                yield doc
            else:
                yield rawdoc

    @wrapper
    def doc_feeder(self, step=None, verbose=True, query=None, scroll='10m', only_source=True, **kwargs):
        step = step or self.step
        q = query if query else {'query': {'match_all': {}}}
        _q_cnt = self.count(q=q, raw=True)
        n = _q_cnt['count']
        n_shards = _q_cnt['_shards']['total']
        assert n_shards == _q_cnt['_shards']['successful']
        # Not sure if scroll size is per shard anymore in the new ES...should check this
        _size = int(step / n_shards)
        assert _size * n_shards == step
        cnt = 0
        t0 = time.time()
        if verbose:
            t1 = time.time()

        res = self._es.search(self._index, self._doc_type, body=q,
                              size=_size, search_type='scan', scroll=scroll, **kwargs)
        # double check initial scroll request returns no hits
        assert len(res['hits']['hits']) == 0

        while 1:
            if verbose:
                t1 = time.time()
            res = self._es.scroll(res['_scroll_id'], scroll=scroll)
            if len(res['hits']['hits']) == 0:
                break
            else:
                for rawdoc in res['hits']['hits']:
                    if rawdoc.get('_source', False) and only_source:
                        doc = rawdoc['_source']
                        doc["_id"] = rawdoc["_id"]
                        yield doc
                    else:
                        yield rawdoc
                    cnt += 1

        assert cnt == n, "Error: scroll query terminated early [{}, {}], please retry.\nLast response:\n{}".format(cnt, n, res)

    @wrapper
    def get_id_list(self, step=None, verbose=True):
        step = step or self.step
        cur = self.doc_feeder(step=step, _source=False, verbose=verbose)
        for doc in cur:
            yield doc['_id']

    @wrapper
    def get_docs(self, ids, step=None, only_source=True, **mget_args):
        ''' Return matching docs for given ids iterable, if not found return None.
            A generator is returned to the matched docs.  If only_source is False,
            the entire document is returned, otherwise only the source is returned. '''
        # chunkify
        step = step or self.step
        for chunk in iter_n(ids, step):
            chunk_res = self._es.mget(body={"ids": chunk}, index=self._index, 
                                      doc_type=self._doc_type, **mget_args)
            for rawdoc in chunk_res['docs']:
                if (('found' not in rawdoc) or (('found' in rawdoc) and not rawdoc['found'])):
                    continue
                elif not only_source:
                    yield rawdoc
                else:
                    doc = rawdoc['_source']
                    doc["_id"] = rawdoc["_id"]
                    yield doc

    def find_biggest_doc(self, fields_li, min=5, return_doc=False):
        """return the doc with the max number of fields from fields_li."""
        for n in range(len(fields_li), min - 1, -1):
            for field_set in itertools.combinations(fields_li, n):
                q = ' AND '.join(["_exists_:" + field for field in field_set])
                q = {'query': {"query_string": {"query": q}}}
                cnt = self.count(q)
                if cnt > 0:
                    if return_doc:
                        res = self._es.search(index=self._index, doc_type=self._doc_type, body=q, size=cnt)
                        return res
                    else:
                        return (cnt, q)

    def snapshot(self,repo,snapshot,mode=None,**params):
        body = {"indices": self._index}
        if mode == "purge":
            try:
                snp = self._es.snapshot.get(repo,snapshot)
                # if we can get it, we have to delete it
                self._es.snapshot.delete(repo,snapshot)
            except NotFoundError:
                # ok, nothing to delete/purge
                pass
        try:
            return self._es.snapshot.create(repo,snapshot,body=body,params=params)
        except RequestError as e:
            raise IndexerException("Can't snapshot '%s' (if already exists, use mode='purge'): %s" % (self._index,e))

    def get_snapshot_status(self,repo,snapshot):
        return self._es.snapshot.status(repo,snapshot)


def generate_es_mapping(inspect_doc,init=True,level=0):
    """Generate an ES mapping according to "inspect_doc", which is 
    produced by biothings.utils.inspect module"""
    map_tpl= {
            int: {"type": "integer"},
            bool: {"type": "boolean"},
            float: {"type": "float"},
            str: {"type": "string","analyzer":"string_lowercase"}, # not splittable (like an ID for instance)
            "split_str": {"type": "string"}
            }
    if init and not "_id" in inspect_doc:
        raise ValueError("Not _id key found, documents won't be indexed")
    mapping = {}
    for rootk in inspect_doc:
        if rootk == "_id":
            continue
        if rootk == "_stats":
            continue
        if rootk == type(None):
            # value can be null, just skip it
            continue
        # some inspect report have True as value, others have dict (will all have dict eventually)
        if inspect_doc[rootk] == True:
            inspect_doc[rootk] = {}
        keys = list(inspect_doc[rootk].keys())
        # if dict, it can be a dict containing the type (no explore needed) or a dict
        # containing more keys (explore needed)
        if list in keys:
            # we explore directly the list w/ inspect_doc[rootk][list] as param. 
            # (similar to skipping list type, as there's no such list type in ES mapping)
            # carefull: there could be list of list, if which we move further into the structure
            # to skip them
            toexplore = inspect_doc[rootk][list]
            while list in toexplore:
                toexplore = toexplore[list]
            res = generate_es_mapping(toexplore,init=False,level=level+1)
            # is it the only key or do we have more ? (ie. some docs have data as "x", some 
            # others have list("x")
            if len(keys) > 1:
            # we want to make sure that, whatever the structure, the types involved were the same
                other_types = set([k for k in keys if k != list and type(k) == type])
                if len(other_types) > 1:
                    raise Exception("Mixing types for key %s: %s" % (rootk,other_types))
            # list was either a list of values (end of tree) or a list of dict. Depending
            # on that, we add "properties" (when list of dict) or not (when list of values)
            if type in set(map(type,inspect_doc[rootk][list])):
                mapping[rootk] = res
            else:
                mapping[rootk] = {"properties" : {}}
                mapping[rootk]["properties"] = res
        elif set(map(type,keys)) == {type}:
            # it's a type declaration, no explore
            typs = list(map(type,keys))
            if len(typs) > 1:
                raise Exception("More than one type")
            try:
                typ = list(inspect_doc[rootk].keys())[0]
                if "split" in inspect_doc[rootk][typ]:
                    typ = "split_str"
                mapping[rootk] = map_tpl[typ]
            except Exception as e:
                raise ValueError("Can't find map type %s for key %s" % (rootk,inspect_doc[rootk]))
        elif inspect_doc[rootk] == {}:
            return map_tpl[rootk]
        else:
            mapping[rootk] = {"properties" : {}}
            mapping[rootk]["properties"] = generate_es_mapping(inspect_doc[rootk],init=False,level=level+1)
    return mapping
