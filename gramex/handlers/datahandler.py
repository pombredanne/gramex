import re
import yaml
import tornado.gen
import tornado.web
import pandas as pd
import sqlalchemy as sa
import gramex
from tornado.web import HTTPError
from orderedattrdict import AttrDict
from six.moves.http_client import NOT_FOUND
from gramex.transforms import build_transform
from .basehandler import BaseHandler

drivers = {}


class DataHandler(BaseHandler):
    '''
    Serves data in specified format from datasource. It accepts these parameters:

    :arg dict kwargs: keyword arguments to be passed to the function.
    :arg string url: The path at which datasource (db, csv) is located.
    :arg string driver: Connector to be used to connect to datasource
        Like -(sqlalchemy, pandas.read_csv, blaze).
        Currently supports sqlalchemy, blaze.
    :arg dict parameters: Additional keyword arguments for driver.
    :arg dict headers: HTTP headers to set on the response.
        Currently supports csv, json, html table

    Here's a simple use -- to return a csv file as a response to a URL. This
    configuration renders `flags` table in `tutorial.db` database as `file.csv`
    at the URL `/datastore/flags`::

        url:
            flags:
                pattern: /datastore/flags                 # Any URL starting with /datastore/flags
                handler: DataHandler                      # uses DataHandler
                kwargs:
                    driver: sqlalchemy                    # Using sqlalchemy driver
                    url: $YAMLPATH/tutorial.db            # Connects to database at this path/url
                    table: flags                          # to this table
                    parameters: {encoding: utf8}          # with additional parameters provided
                    default: {}                           # default query parameters
                    query: {}                             # query parameter overrides
                    headers:
                        Content-Type: text/csv            # and served as csv
                        # Content-Type: application/json  # or JSON
                        # Content-Type: text/html         # or HTML

    '''
    @classmethod
    def setup(cls, **kwargs):
        super(DataHandler, cls).setup(**kwargs)
        cls.params = AttrDict(kwargs)
        cls.driver_key = yaml.dump(kwargs)

        driver = kwargs.get('driver')
        cls.driver_name = driver
        if driver in ['sqlalchemy', 'blaze']:
            cls.driver_method = getattr(cls, '_' + driver)
        else:
            raise NotImplementedError('driver=%s is not supported yet.' % driver)

        posttransform = kwargs.get('posttransform', {})
        cls.posttransform = []
        if 'function' in posttransform:
            cls.posttransform.append(
                build_transform(
                    posttransform, vars=AttrDict(content=None),
                    filename='url>%s' % cls.name))

        qconfig = {'query': cls.params.get('query', {}),
                   'default': cls.params.get('default', {})}
        delims = {'agg': ':', 'sort': ':', 'where': ''}
        nojoins = ['select', 'groupby']

        for q in qconfig:
            if qconfig[q]:
                tmp = AttrDict()
                for key in qconfig[q].keys():
                    val = qconfig[q][key]
                    if isinstance(val, list):
                        tmp[key] = val
                    elif isinstance(val, dict):
                        tmp[key] = [k if key in nojoins
                                    else k + delims[key] + v
                                    for k, v in val.items()]
                    elif isinstance(val, (str, int)):
                        tmp[key] = [val]
                qconfig[q] = tmp
        cls.qconfig = qconfig

    def initialize(self, **kwargs):
        super(DataHandler, self).initialize(**kwargs)
        # Set the method to the ?x-http-method-overrride argument or the
        # X-HTTP-Method-Override header if they exist
        if 'x-http-method-override' in self.request.arguments:
            self.request.method = self.get_argument('x-http-method-override')
        elif 'X-HTTP-Method-Override' in self.request.headers:
            self.request.method = self.request.headers['X-HTTP-Method-Override']

    def getq(self, key, default_value=None):
        return (self.qconfig['query'].get(key) or
                self.get_arguments(key) or
                self.qconfig['default'].get(key) or
                default_value)

    def _sqlalchemy_gettable(self):
        if self.driver_key not in drivers:
            parameters = self.params.get('parameters', {})
            drivers[self.driver_key] = sa.create_engine(self.params['url'], **parameters)
        self.driver_engine = drivers[self.driver_key]

        meta = sa.MetaData(bind=self.driver_engine, reflect=True)
        table = meta.tables[self.params['table']]
        return table

    def _sqlalchemy_wheres(self, _wheres, table):
        wh_re = re.compile(r'([^=><~!]+)([=><~!]{1,2})([\s\S]+)')
        wheres = []
        for where in _wheres:
            match = wh_re.search(where)
            if match is None:
                continue
            col, oper, val = match.groups()
            col = table.c[col]
            if oper in ['==', '=']:
                wheres.append(col == val)
            elif oper == '>=':
                wheres.append(col >= val)
            elif oper == '<=':
                wheres.append(col <= val)
            elif oper == '>':
                wheres.append(col > val)
            elif oper == '<':
                wheres.append(col < val)
            elif oper == '!=':
                wheres.append(col != val)
            elif oper == '~':
                wheres.append(col.ilike('%' + val + '%'))
            elif oper == '!~':
                wheres.append(col.notlike('%' + val + '%'))
        wheres = sa.and_(*wheres)
        return wheres

    def _sqlalchemy(self, _selects, _wheres, _groups, _aggs, _offset, _limit, _sorts):
        table = self._sqlalchemy_gettable()

        if _wheres:
            wheres = self._sqlalchemy_wheres(_wheres, table)

        if _groups and _aggs:
            grps = [table.c[c] for c in _groups]
            aggselects = grps[:]
            safuncs = {'min': sa.func.min, 'max': sa.func.max,
                       'sum': sa.func.sum, 'count': sa.func.count,
                       'mean': sa.func.avg, 'nunique': sa.func.count}
            agg_re = re.compile(r'([^:]+):([aA-zZ]+)\(([^:]+)\)')
            for agg in _aggs:
                match = agg_re.search(agg)
                if match is None:
                    continue
                name, oper, col = match.groups()
                if oper == 'nunique':
                    aggselects.append(sa.func.count(table.c[col].distinct()).label(name))
                else:
                    aggselects.append(safuncs[oper](table.c[col]).label(name))

            if _selects:
                aggselects = [grp for grp in aggselects if grp.key in _selects]

            query = sa.select(aggselects)
            if _wheres:
                query = query.where(wheres)
            query = query.group_by(*grps)
        else:
            if _selects:
                query = sa.select([table.c[c] for c in _selects])
            else:
                query = sa.select([table])
            if _wheres:
                query = query.where(wheres)

        if _sorts:
            order = {'asc': sa.asc, 'desc': sa.desc}
            sorts = []
            for sort in _sorts:
                col, odr = sort.partition(':')[::2]
                sorts.append(order.get(odr, sa.asc)(col))
            query = query.order_by(*sorts)

        if _offset:
            query = query.offset(_offset)
        if _limit:
            query = query.limit(_limit)

        return pd.read_sql_query(query, self.driver_engine)

    def _sqlalchemy_post(self, _vals):
        table = self._sqlalchemy_gettable()
        content = dict(x.split('=', 1) for x in _vals)
        for posttransform in self.posttransform:
            for value in posttransform(content):
                content = value
        self.driver_engine.execute(table.insert(), **content)
        return pd.DataFrame()

    def _sqlalchemy_delete(self, _wheres):
        if not _wheres:
            raise HTTPError(NOT_FOUND, log_message='WHERE is required in DELETE method')
        table = self._sqlalchemy_gettable()
        wheres = self._sqlalchemy_wheres(_wheres, table)
        self.driver_engine.execute(table.delete().where(wheres))
        return pd.DataFrame()

    def _sqlalchemy_put(self, _vals, _wheres):
        if not _vals:
            raise HTTPError(NOT_FOUND, log_message='VALS is required in PUT method')
        if not _wheres:
            raise HTTPError(NOT_FOUND, log_message='WHERE is required in PUT method')
        table = self._sqlalchemy_gettable()
        content = dict(x.split('=', 1) for x in _vals)
        wheres = self._sqlalchemy_wheres(_wheres, table)
        self.driver_engine.execute(table.update().where(wheres).values(content))
        return pd.DataFrame()

    def _blaze(self, _selects, _wheres, _groups, _aggs, _offset, _limit, _sorts):
        # Import blaze on demand -- it's a very slow import
        import blaze as bz                      # noqa

        # TODO: Not caching blaze connections
        parameters = self.params.get('parameters', {})
        bzcon = bz.Data(self.params['url'] +
                        ('::' + self.params['table'] if self.params.get('table') else ''),
                        **parameters)
        table = bz.TableSymbol('table', bzcon.dshape)
        query = table

        if _wheres:
            wh_re = re.compile(r'([^=><~!]+)([=><~!]{1,2})([\s\S]+)')
            wheres = None
            for where in _wheres:
                match = wh_re.search(where)
                if match is None:
                    continue
                col, oper, val = match.groups()
                col = table[col]
                if oper in ['==', '=']:
                    whr = (col == val)
                elif oper == '>=':
                    whr = (col >= val)
                elif oper == '<=':
                    whr = (col <= val)
                elif oper == '>':
                    whr = (col > val)
                elif oper == '<':
                    whr = (col < val)
                elif oper == '!=':
                    whr = (col != val)
                elif oper == '~':
                    whr = (col.like('*' + val + '*'))
                elif oper == '!~':
                    whr = (~col.like('*' + val + '*'))
                wheres = whr if wheres is None else wheres & whr
            query = query[wheres]

        if _groups and _aggs:
            byaggs = {'min': bz.min, 'max': bz.max,
                      'sum': bz.sum, 'count': bz.count,
                      'mean': bz.mean, 'nunique': bz.nunique}
            agg_re = re.compile(r'([^:]+):([aA-zZ]+)\(([^:]+)\)')
            grps = bz.merge(*[query[group] for group in _groups])
            aggs = {}
            for agg in _aggs:
                match = agg_re.search(agg)
                if match is None:
                    continue
                name, oper, col = match.groups()
                aggs[name] = byaggs[oper](query[col])
            query = bz.by(grps, **aggs)

        if _sorts:
            order = {'asc': True, 'desc': False}
            sorts = []
            for sort in _sorts:
                col, odr = sort.partition(':')[::2]
                sorts.append(col)
            query = query.sort(sorts, ascending=order.get(odr, True))

        if _offset:
            _offset = int(_offset)
        if _limit:
            _limit = int(_limit)
        if _offset and _limit:
            _limit += _offset
        if _offset or _limit:
            query = query[_offset:_limit]

        if _selects:
            query = query[_selects]

        # TODO: Improve json, csv, html outputs using native odo
        return bz.odo(bz.compute(query, bzcon.data), pd.DataFrame)

    def _render(self):
        # Set content and type based on format
        formats = self.getq('format', ['json'])
        if 'json' in formats:
            self.set_header('Content-Type', 'application/json')
            self.content = self.result.to_json(orient='records')
        elif 'csv' in formats:
            self.set_header('Content-Type', 'text/csv')
            self.set_header("Content-Disposition", "attachment;filename=file.csv")
            self.content = self.result.to_csv(index=False, encoding='utf-8')
        elif 'html' in formats:
            self.set_header('Content-Type', 'text/html')
            self.content = self.result.to_html()
        else:
            raise NotImplementedError('format=%s is not supported yet.' % formats)

        # Allow headers to be overridden
        for header_name, header_value in self.params.get('headers', {}).items():
            self.set_header(header_name, header_value)

    @tornado.gen.coroutine
    def get(self):
        kwargs = dict(
            _selects=self.getq('select'),
            _wheres=self.getq('where'),
            _groups=self.getq('groupby'),
            _aggs=self.getq('agg'),
            _offset=self.getq('offset', [None])[0],
            _limit=self.getq('limit', [100])[0],
            _sorts=self.getq('sort'),
        )

        self.result = yield gramex.service.threadpool.submit(self.driver_method, **kwargs)
        self._render()
        self.write(self.content)

    @tornado.gen.coroutine
    def post(self):
        if self.driver_name != 'sqlalchemy':
            raise NotImplementedError('driver=%s is not supported yet.' % self.driver_name)
        kwargs = {'_vals': self.getq('val', [])}
        self.result = yield gramex.service.threadpool.submit(self._sqlalchemy_post, **kwargs)
        self._render()
        self.write(self.content)

    @tornado.gen.coroutine
    def delete(self):
        if self.driver_name != 'sqlalchemy':
            raise NotImplementedError('driver=%s is not supported yet.' % self.driver_name)
        kwargs = {'_wheres': self.getq('where')}
        self.result = yield gramex.service.threadpool.submit(self._sqlalchemy_delete, **kwargs)
        self._render()
        self.write(self.content)

    @tornado.gen.coroutine
    def put(self):
        if self.driver_name != 'sqlalchemy':
            raise NotImplementedError('driver=%s is not supported yet.' % self.driver_name)
        kwargs = {'_vals': self.getq('val', []), '_wheres': self.getq('where')}
        self.result = yield gramex.service.threadpool.submit(self._sqlalchemy_put, **kwargs)
        self._render()
        self.write(self.content)
