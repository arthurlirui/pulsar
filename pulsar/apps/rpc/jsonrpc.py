'''\
A Python implementation of JSON-RPC 2.0 Specification

JSON-RPC is a lightweight remote procedure call protocol.
It's designed to be simple.

A remote method is invoked by sending a request to a remote service.
The request is a single object serialized using JSON.

The specification is at
http://groups.google.com/group/json-rpc/web/json-rpc-2-0
'''
import sys
from functools import partial

from pulsar import (Failure, multi_async, maybe_failure, in_loop_thread,
                    coroutine_return)
from pulsar.utils.system import json
from pulsar.utils.structures import AttributeDictionary
from pulsar.utils.security import gen_unique_id
from pulsar.utils.pep import range
from pulsar.utils.tools import checkarity
from pulsar.apps.wsgi import Json
from pulsar.apps.http import HttpClient
from pulsar.utils.httpurl import JSON_CONTENT_TYPES

from .handlers import RpcHandler, InvalidRequest, exception

__all__ = ['JSONRPC', 'JsonProxy']


class JSONRPC(RpcHandler):
    '''An :class:`RpcHandler` for class for JSON-RPC services.
Design to comply with the `JSON-RPC 2.0`_ Specification.

.. _`JSON-RPC 2.0`: http://www.jsonrpc.org/
'''
    version = '2.0'
    methods = ('post',)

    def __call__(self, request):
        return Json(self._call(request)).http_response(request)

    def _call(self, request):
        response = request.response
        data = {}
        try:
            try:
                data = yield request.body_data()
            except ValueError:
                raise InvalidRequest(
                    status=415, msg='Content-Type must be application/json')
            if data.get('jsonrpc') != self.version:
                raise InvalidRequest(
                    'jsonrpc must be supplied and equal to "%s"' %
                    self.version)
            params = data.get('params')
            if isinstance(params, dict):
                args, kwargs = (), params
            else:
                args, kwargs = tuple(params or ()), {}
            #
            callable = self.get_handler(data.get('method'))
            result = yield callable(request, *args, **kwargs)
        except Exception:
            result = sys.exc_info()
        #
        result = maybe_failure(result)
        res = {'id': data.get('id'), "jsonrpc": self.version}
        if isinstance(result, Failure):
            msg = None
            error = result.error
            code = getattr(error, 'fault_code', -32603)
            if isinstance(error, TypeError):
                msg = checkarity(callable, args, kwargs, discount=1)
            msg = msg or str(error) or 'JSON RPC exception'
            if code == -32603:
                result.log(msg=msg, level='error')
            else:
                result.log(msg=msg, level='warning')
            result = {'code': code,
                      'message': msg,
                      'data': getattr(error, 'data', '')}
            request.response.status_code = getattr(error, 'status', 500)
            res['error'] = result
        else:
            res['result'] = result
        yield res


class JsonCall:
    slots = ('_client', '_name')

    def __init__(self, client, name):
        self._client = client
        self._name = name

    def __repr__(self):
        return self._name
    __str__ = __repr__

    @property
    def url(self):
        return self._client.url

    @property
    def name(self):
        return self._name

    def __getattr__(self, name):
        name = "%s%s%s" % (self._name, self._client.separator, name)
        return self.__class__(self._client, name)

    def __call__(self, *args, **kwargs):
        return self._client._call(self._name, *args, **kwargs)


class JsonProxy(object):
    '''A python Proxy class for :class:`JSONRPC` Servers.

    :param url: server location
    :param version: JSONRPC server version. Default ``2.0``
    :param id: optional request id, generated if not provided.
        Default ``None``.
    :param data: Extra data to include in all requests. Default ``None``.
    :param full_response: return the full Http response rather than
        just the content.
    :param http: optional http client. If provided it must have the ``request``
        method available which must be of the form::

            http.request(url, body=..., method=...)

        Default ``None``.

    Lets say your RPC server is running at ``http://domain.name.com/``::

        >>> a = JsonProxy('http://domain.name.com/')
        >>> a.add(3,4)
        7
        >>> a.ping()
        'pong'

    '''
    separator = '.'
    default_version = '2.0'
    default_timeout = 30

    def __init__(self, url, version=None, data=None,
                 full_response=False, http=None, timeout=None, **kw):
        self._url = url
        self._version = version or self.__class__.default_version
        self._full_response = full_response
        self._data = data if data is not None else {}
        if not http:
            timeout = timeout if timeout is not None else self.default_timeout
            http = HttpClient(timeout=timeout, **kw)
        http.headers['accept'] = 'application/json, text/*; q=0.5'
        http.headers['content-type'] = 'application/json'
        self._http = http

    @property
    def url(self):
        return self._url

    @property
    def version(self):
        return self._version

    @property
    def _loop(self):
        return self._http._loop

    def makeid(self):
        '''Can be re-implemented by your own Proxy'''
        return gen_unique_id()

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self.__url)

    def __str__(self):
        return self.__repr__()

    def __getattr__(self, name):
        return JsonCall(self, name)

    def timeit(self, func, times, *args, **kwargs):
        '''Useful utility for timing responses from a server.

        The usage is simple::

            >>> from pulsar.apps import rpc
            >>> p = rpc.JsonProxy('http://127.0.0.1:8060')
            >>> p.timeit('ping', 10)
            0.56...
        '''
        func = getattr(self, func)
        return multi_async((func(*args, **kwargs) for t in range(times)))

    @in_loop_thread
    def _call(self, name, *args, **kwargs):
        data = self._get_data(name, *args, **kwargs)
        body = json.dumps(data).encode('utf-8')
        resp = yield self._http._request('POST', self._url, data=body)
        if self._full_response:
            coroutine_return(resp)
        else:
            content = resp.decode_content()
            if resp.is_error:
                if 'error' not in content:
                    resp.raise_for_status()
            coroutine_return(self.loads(content))

    def _get_data(self, func_name, *args, **kwargs):
        id = self.makeid()
        params = self.get_params(*args, **kwargs)
        data = {'method': func_name, 'params': params, 'id': id,
                'jsonrpc': self._version}
        return data

    def get_params(self, *args, **kwargs):
        '''
        Create an array or positional or named parameters
        Mixing positional and named parameters in one
        call is not possible.
        '''
        kwargs.update(self._data)
        if args and kwargs:
            raise ValueError('Cannot mix positional and named parameters')
        if args:
            return list(args)
        else:
            return kwargs

    def loads(self, obj):
        if isinstance(obj, dict):
            if 'error' in obj:
                error = obj['error']
                raise exception(error.get('code'), error.get('message'))
            else:
                return obj.get('result')
        return obj
