'''
Greenlet support facilitates the integration of synchronous
third-party libraries into pulsar asynchronous framework.
It requires the :greenlet:`greenlet <>` library.

If you want to understand how integration works but you are unfamiliar with
greenlets, check out the :greenlet:`greenlet documentation <>` first.
On the other hand,
if you need to use it in the context of :ref:`asynchronous psycopg2 <psycopg2>`
connections for example, you can skip the implementation details.

This application **does not use monkey patching** and therefore it
works quite differently from implicit asynchronous libraries such as
gevent_. All it does, it provides the user with a limited set
of utilities for **explicitly** transferring execution from one greenlet
to a another which execute the blocking call in a greenlet-friendly way.

The caller has the responsibility that the blocking call is greenlet-friendly,
i.e. it transfers the control of execution back to the parent greenlet when
needed.

Usage
=======

Lets assume you are building an application which uses pulsar asynchronous
engine and would like to

* either use an external library written in blocking style,
  i.e. without yielding control to the event loop when IO calls are performed.
* or write your client code without dealing with :class:`~asyncio.Future` or
  coroutines, in other words in an implicit asynchronous style. In this way
  your client code can be used on other frameworks just as well.

In both cases, the :class:`~pulsar.apps.greenio` application is what you need.

.. _green-wsgi:

Green WSGI
---------------

Assume you are using pulsar web server and would like to write your application
in an implicit asynchronous mode, i.e. without dealing with futures nor
coroutines, then you can wrap your wsgi ``app`` with the :class:`.RunInPool`
utility::

    from pulsar.apps import wsgi, greenio

    callable = wsgi.WsgiHandler([wsgi.wait_for_body_middleware,
                                 greenio.RunInPool(app, 20)])

    wsgi.WsgiServer(callable=callable).start()

The :class:`.RunInPool` manages a pool of greenlets which execute your
application. In this way, within your ``app`` you can invoke the
:func:`.wait` function when needing to wait for asynchronous results to be
ready.

.. _green-http:

Green Http
-----------------

The :class:`.HttpClient` can be used with greenlets::

    >>> from pulsar.apps.http import HttpClient
    >>> http = HttpClient(green=True)
    >>> http.green
    True

And now you can write synchronous looking code and run it in a separate
greenlet via the :func:`.run_in_greenlet` decorator::

    @greenio.run_in_greenlet
    def example():
        response = http.get('http://bbc.co.uk')
        ...
        return 'done'


and somewhere, in your asynchronous code::

        result = yield example()
        result == 'done'


the :func:`.run_in_greenlet` decorator, execute the function on a child
greenlet without blocking the asynchronous engine. Once the ``example``
function returns, the asynchronous code continue from the ``yield``
statement as usual.


API
======

Wait
----------

.. autofunction:: wait

Wait file descriptor
-----------------------

.. autofunction:: wait_fd


Run in greenlet
-------------------

.. autofunction:: run_in_greenlet


Green task
-------------

.. autofunction:: green_task


.. module:: pulsar.apps.greenio.pool

Green Pool
----------------

.. autoclass:: GreenPool
   :members:
   :member-order: bysource

Run in Pool
----------------

.. autoclass:: RunInPool
   :members:
   :member-order: bysource


.. _psycopg2:

Psycopg2
===========

.. automodule:: pulsar.apps.greenio.pg


.. _gevent: http://www.gevent.org/
'''
import threading
from collections import deque
from functools import wraps, partial

import greenlet
from greenlet import getcurrent

from pulsar import isfuture, async

from pulsar import Future, get_event_loop, AsyncObject, async, is_async


_DEFAULT_WORKERS = 100
_MAX_WORKERS = 1000


class GreenletWorker(greenlet.greenlet):
    pass


def wait(value):
    '''Wait for a possible asynchronous value to complete.
    '''
    current = greenlet.getcurrent()
    parent = current.parent
    return parent.switch(value) if parent else value


def run_in_greenlet(callable):
    '''Decorator to run a ``callable`` on a new greenlet.

    A ``callable`` decorated with this decorator returns a coroutine
    '''
    @wraps(callable)
    def _(*args, **kwargs):
        gr = GreenletWorker(callable)
        # switch to the new greenlet
        result = gr.switch(*args, **kwargs)
        # back to the parent
        while isfuture(result):
            # keep on switching back to the greenlet if we get a Future
            result = gr.switch((yield from result))
        # For some reason this line does not show in coverage reports
        # but it is covered!
        return result    # pragma nocover

    return _


class GreenPool(AsyncObject):
    '''A pool of running greenlets.

    This pool maintains a group of greenlets to perform asynchronous
    tasks via the :meth:`submit` method.
    '''
    worker_name = 'exec'

    def __init__(self, max_workers=None, loop=None, maxtasks=None):
        self._loop = loop or get_event_loop()
        self._max_workers = min(max_workers or _DEFAULT_WORKERS, _MAX_WORKERS)
        self._greenlets = set()
        self._available = set()
        self._maxtasks = maxtasks
        self._queue = deque()
        self._shutdown = False
        self._waiter = None
        self._shutdown_lock = threading.Lock()

    def submit(self, func, *args, **kwargs):
        '''Equivalent to ``func(*args, **kwargs)``.

        This method create a new task for function ``func`` and adds it to
        the queue.
        Return a :class:`~asyncio.Future` called back once the task
        has finished.
        '''
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError(
                    'cannot schedule new futures after shutdown')
            future = Future(loop=self._loop)
            self._put((future, func, args, kwargs))
            return future

    def shutdown(self, wait=True):
        with self._shutdown_lock:
            self._shutdown = True
            self._put()
            if wait:
                self._waiter = Future(loop=self._loop)
                return self._waiter

    # INTERNALS
    def _adjust_greenlet_count(self):
        if len(self._greenlets) < self._max_workers:
            greenlet = GreenletWorker(self._green_run)
            self._greenlets.add(greenlet)
            greenlet.switch()

    def _put(self, task=None):
        # Run in the main greenlet of the evnet-loop thread
        if task:
            self._adjust_greenlet_count()
        self._queue.appendleft(task)
        self._check_queue()

    def _check_queue(self):
        # Run in the main greenlet of the evnet-loop thread
        try:
            task = self._queue.pop()
        except IndexError:
            return
        if self._available:
            greenlet = self._available.pop()
            async(self._green_task(greenlet, task), loop=self._loop)

    def _green_task(self, greenlet, task):
        # Run in the main greenlet of the evnet-loop thread
        result = greenlet.switch(task)
        while is_async(result):
            result = greenlet.switch((yield from result))

    def _green_run(self):
        # The run method of a worker greenlet
        task = True
        while task:
            greenlet = getcurrent()
            parent = greenlet.parent
            assert parent
            self._available.add(greenlet)
            self._loop.call_soon(self._check_queue)
            task = parent.switch()  # switch back to the main execution
            if task:
                # If a new task is available execute it
                # Here we are in the child greenlet
                future, func, args, kwargs = task
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            else:
                self._greenlets.remove(greenlet)
                if self._greenlets:
                    self._put(None)
                elif self._waiter:
                    self._waiter.set_result(None)
                    self._waiter = None


class RunInPool:
    '''Utility for running a callable in a :class:`.GreenPool`.

    :param app: the callable to run on greenlet workers
    :param max_workers=100: maximum number of workers
    :param loop: optional event loop

    THis utility is used by the :mod:`~pulsar.apps.pulse` application.
    '''
    def __init__(self, app, max_workers=None, loop=None):
        self.pool = GreenPool(max_workers=max_workers, loop=loop)
        self.app = app

    def __call__(self, *args, **kwargs):
        return self.pool.submit(self.app, *args, **kwargs)
