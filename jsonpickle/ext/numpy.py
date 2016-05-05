# -*- coding: utf-8 -*-

from __future__ import absolute_import
from builtins import *

import zlib
import warnings

import numpy as np

import ast
import jsonpickle
from jsonpickle.compat import unicode

__all__ = ['register_handlers', 'unregister_handlers']


class NumpyBaseHandler(jsonpickle.handlers.BaseHandler):

    def restore_dtype(self, data):
        dtype = data['dtype']
        if dtype.startswith(('{', '[')):
            return ast.literal_eval(dtype)
        return np.dtype(dtype)

    def flatten_dtype(self, dtype, data):

        if hasattr(dtype, 'tostring'):
            data['dtype'] = dtype.tostring()
        else:
            dtype = unicode(dtype)
            prefix = '(numpy.record, '
            if dtype.startswith(prefix):
                dtype = dtype[len(prefix):-1]
            data['dtype'] = dtype


class NumpyDTypeHandler(NumpyBaseHandler):

    def flatten(self, obj, data):
        self.flatten_dtype(obj, data)
        return data

    def restore(self, data):
        return self.restore_dtype(data)


class NumpyGenericHandler(NumpyBaseHandler):

    def flatten(self, obj, data):
        self.flatten_dtype(obj.dtype, data)
        data['value'] = self.context.flatten(obj.tolist(), reset=False)
        return data

    def restore(self, data):
        value = self.context.restore(data['value'], reset=False)
        return self.restore_dtype(data).type(value)


class NumpyNDArrayHandler(NumpyBaseHandler):

    def flatten(self, obj, data):
        self.flatten_dtype(obj.dtype, data)
        data['values'] = self.context.flatten(obj.tolist(), reset=False)
        if 0 in obj.shape:
            # add shape information explicitly as it cannot be determined from an empty list
            data['shape'] = obj.shape
        if obj.flags.f_contiguous:
            data['order'] = 'F'
        return data

    def restore(self, data):
        values = self.context.restore(data['values'], reset=False)
        arr = np.array(
            values,
            dtype=self.restore_dtype(data),
            order=data.get('order', 'C')
        )
        shape = data.get('shape', None)
        if shape is not None:
            arr = arr.reshape(shape)
        return arr


class NumpyNDArrayHandlerBinary(NumpyNDArrayHandler):
    """stores arrays with size greater than 'size_treshold' as compressed base64

    valid values for 'compression' are {zlib, bz2, None}
    if compresion is None, no compression is applied

    valid values for 'size_treshold' are all nonnegative integers and None
    if size_treshold is None, values are always stored as nested lists
    """

    size_treshold = 16
    compression = zlib

    def flatten(self, obj, data):
        """encode numpy to json"""
        if self.size_treshold > obj.size or self.size_treshold is None:
            # store as json
            data = super(NumpyNDArrayHandlerBinary, self).flatten(obj, data)
        else:
            # store as binary
            self.flatten_dtype(obj.dtype, data)
            buffer = obj.tobytes(order=None)
            if self.compression:
                buffer = self.compression.compress(buffer)
            values = jsonpickle.util.b64encode(buffer)
            data['values'] = self.context.flatten(values, reset=False)
            data['shape'] = obj.shape
            if obj.flags.f_contiguous:
                data['order'] = 'F'
        return data

    def restore(self, data):
        """decode numpy from json"""
        values = data['values']
        if isinstance(values, list):
            # decode json
            arr = super(NumpyNDArrayHandlerBinary, self).restore(data)
        else:
            # decode binary representation
            values = self.context.restore(values, reset=False)
            buffer = jsonpickle.util.b64decode(values)
            if self.compression:
                buffer = self.compression.decompress(buffer)
            arr = np.ndarray(
                buffer=buffer,
                dtype=self.restore_dtype(data),
                shape=data.get('shape'),
                order=data.get('order', 'C')
            ).copy() # make a copy, to force the result to own the data

        return arr


class NumpyNDArrayHandlerView(NumpyNDArrayHandlerBinary):
    """Pickles references inside ndarrays, or array-views

    Notes
    -----
    The current implementation has some restrictions.

    'base' arrays, or arrays which are viewed by other arrays, must be c-contiguous.
    This is not such a large restriction in practice, because all numpy array creation is c-contiguous by default.
    Releasing this restriction would be nice though; especially if it can be done without bloating the design too much.

    Furthermore, ndarrays which are views of array-like objects implementing __array_interface__,
    but which are not themselves nd-arrays, are deepcopied with a warning,
    as we cannot guarantee whatever custom logic such classes implement is correctly reproduced.
    """
    mode = 'warn'

    def flatten(self, obj, data):
        """encode numpy to json"""
        base = obj.base
        if base is None:
            # store by value
            data = super(NumpyNDArrayHandlerView, self).flatten(obj, data)
        elif isinstance(base, np.ndarray) and base.data.contiguous:
            # store by reference
            self.flatten_dtype(obj.dtype, data)
            data['base'] = self.context.flatten(base, reset=False)
            # for a view, always store shape
            data['shape'] = obj.shape

            offset = obj.ctypes.data - base.ctypes.data
            if offset:
                data['offset'] = offset

            if not obj.data.c_contiguous:
                data['strides'] = obj.strides
        else:
            # store a deepcopy or fail
            if self.mode == 'warn':
                msg = "ndarray is defined by reference to an object we do not know how to serialize. " \
                      "A deep copy is serialized instead, breaking memory aliasing."
                warnings.warn(msg)
            elif self.mode == 'raise':
                msg = "ndarray is defined by reference to an object we do not know how to serialize."
                raise ValueError(msg)
            data = super(NumpyNDArrayHandlerView, self).flatten(obj.copy(), data)

        return data

    def restore(self, data):
        """decode numpy from json"""
        base = data.get('base', None)
        if base is None:
            # decode array with owndata=True
            arr = super(NumpyNDArrayHandlerView, self).restore(data)
        else:
            # decode array view, which references the data of another array
            base = self.context.restore(base, reset=False)
            assert base.data.contiguous, \
                "Current implementation assumes base is contiguous"

            arr = np.ndarray(
                buffer=base.data,
                dtype=self.restore_dtype(data),
                shape=data.get('shape'),
                offset=data.get('offset', 0),
                strides=data.get('strides', None)
            )

        return arr


def register_handlers():
    jsonpickle.handlers.register(np.dtype, NumpyDTypeHandler, base=True)
    jsonpickle.handlers.register(np.generic, NumpyGenericHandler, base=True)
    jsonpickle.handlers.register(np.ndarray, NumpyNDArrayHandlerView, base=True)


def unregister_handlers():
    jsonpickle.handlers.unregister(np.dtype)
    jsonpickle.handlers.unregister(np.generic)
    jsonpickle.handlers.unregister(np.ndarray)
