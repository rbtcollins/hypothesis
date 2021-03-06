# coding=utf-8
#
# This file is part of Hypothesis (https://github.com/DRMacIver/hypothesis)
#
# Most of this work is copyright (C) 2013-2015 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# https://github.com/DRMacIver/hypothesis/blob/master/CONTRIBUTING.rst for a
# full list of people who may hold copyright, and consult the git log if you
# need to determine who owns an individual contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.
#
# END HEADER

"""This file can approximately be considered the collection of hypothesis going
to really unreasonable lengths to produce pretty output."""


from __future__ import division, print_function, absolute_import

import os
import re
import ast
import sys
import uuid
import types
import hashlib
import inspect
from functools import wraps
from contextlib import contextmanager

from six import u

from hypothesis.settings import storage_directory
from hypothesis.internal.compat import hrange, qualname, text_type, \
    getargspec, to_unicode, isidentifier, unicode_safe_repr, \
    ARG_NAME_ATTRIBUTE, update_code_location, \
    importlib_invalidate_caches


def fully_qualified_name(f):
    """Returns a unique identifier for f pointing to the module it was define
    on, and an containing functions."""
    return f.__module__ + u('.') + qualname(f)


def function_digest(function):
    """Returns a string that is stable across multiple invocations across
    multiple processes and is prone to changing significantly in response to
    minor changes to the function.

    No guarantee of uniqueness though it usually will be.

    """
    hasher = hashlib.md5()
    try:
        hasher.update(to_unicode(inspect.getsource(function)).encode(u('utf-8')))
    # Different errors on different versions of python. What fun.
    except (OSError, IOError, TypeError):
        pass
    try:
        hasher.update(function.__name__.encode(u('utf-8')))
    except AttributeError:
        pass
    try:
        hasher.update(function.__module__.__name__.encode(u('utf-8')))
    except AttributeError:
        pass
    try:
        hasher.update(repr(getargspec(function)).encode(u('utf-8')))
    except TypeError:
        pass
    return hasher.digest()


def convert_keyword_arguments(function, args, kwargs):
    """Returns a pair of a tuple and a dictionary which would be equivalent
    passed as positional and keyword args to the function. Unless function has.

    **kwargs the dictionary will always be empty.

    """
    argspec = getargspec(function)
    new_args = []
    kwargs = dict(kwargs)

    defaults = {}

    if argspec.defaults:
        for name, value in zip(
                argspec.args[-len(argspec.defaults):],
                argspec.defaults
        ):
            defaults[name] = value

    n = max(len(args), len(argspec.args))

    for i in hrange(n):
        if i < len(args):
            new_args.append(args[i])
        else:
            arg_name = argspec.args[i]
            if arg_name in kwargs:
                new_args.append(kwargs.pop(arg_name))
            elif arg_name in defaults:
                new_args.append(defaults[arg_name])
            else:
                raise TypeError(u('No value provided for argument %r') % (
                    arg_name
                ))

    if kwargs and not argspec.keywords:
        if len(kwargs) > 1:
            raise TypeError(u('%s() got unexpected keyword arguments %s') % (
                function.__name__, u(', ').join(map(repr, kwargs))
            ))
        else:
            bad_kwarg = next(iter(kwargs))
            raise TypeError(u('%s() got an unexpected keyword argument %r') % (
                function.__name__, bad_kwarg
            ))
    return tuple(new_args), kwargs


def convert_positional_arguments(function, args, kwargs):
    """Return a tuple (new_args, new_kwargs) where all possible arguments have
    been moved to kwargs.

    new_args will only be non-empty if function has a
    variadic argument.

    """
    argspec = getargspec(function)
    kwargs = dict(kwargs)
    if not argspec.keywords:
        for k in kwargs.keys():
            if k not in argspec.args:
                raise TypeError(
                    u('%s() got an unexpected keyword argument %r') % (
                        function.__name__, k
                    ))
    if len(args) < len(argspec.args):
        for i in hrange(
            len(args), len(argspec.args) - len(argspec.defaults or ())
        ):
            if argspec.args[i] not in kwargs:
                raise TypeError(u('No value provided for argument %s') % (
                    argspec.args[i],
                ))

    if len(args) > len(argspec.args) and not argspec.varargs:
        raise TypeError(
            u('%s() takes at most %d positional arguments (%d given)') % (
                function.__name__, len(argspec.args), len(args)
            )
        )

    for arg, name in zip(args, argspec.args):
        if name in kwargs:
            raise TypeError(
                u('%s() got multiple values for keyword argument %r') % (
                    function.__name__, name
                ))
        else:
            kwargs[name] = arg
    return (
        tuple(args[len(argspec.args):]),
        kwargs,
    )


def extract_all_lambdas(tree):
    lambdas = []

    class Visitor(ast.NodeVisitor):

        def visit_Lambda(self, node):
            lambdas.append(node)

    Visitor().visit(tree)

    return lambdas


def args_for_lambda_ast(l):
    return [getattr(n, ARG_NAME_ATTRIBUTE) for n in l.args.args]


LINE_CONTINUATION = re.compile(r"\\\n")
WHITESPACE = re.compile(r"\s+")
PROBABLY_A_COMMENT = re.compile("""#[^'"]*$""")
SPACE_FOLLOWS_OPEN_BRACKET = re.compile(r"\( ")
SPACE_PRECEDES_CLOSE_BRACKET = re.compile(r"\( ")


def extract_lambda_source(f):
    """Extracts a single lambda expression from the string source. Returns a
    string indicating an unknown body if it gets confused in any way.

    This is not a good function and I am sorry for it. Forgive me my
    sins, oh lord

    """
    args = getargspec(f).args
    arg_strings = []
    # In Python 2 you can have destructuring arguments to functions. This
    # results in an argspec with non-string values. I'm not very interested in
    # handling these properly, but it's important to not crash on them.
    bad_lambda = False
    for a in args:
        if isinstance(a, (tuple, list)):  # pragma: no cover
            arg_strings.append(u('(%s)') % (u(', ').join(a),))
            bad_lambda = True
        else:
            assert isinstance(a, str)
            arg_strings.append(a)

    if_confused = u('lambda %s: <unknown>') % (u(', ').join(arg_strings),)
    if bad_lambda:  # pragma: no cover
        return if_confused
    try:
        source = inspect.getsource(f)
    except IOError:
        return if_confused

    if not isinstance(source, text_type):  # pragma: no branch
        source = source.decode(u('utf-8'))  # pragma: no cover
    source = LINE_CONTINUATION.sub(u(' '), source)
    source = WHITESPACE.sub(u(' '), source)
    source = source.strip()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        for i in hrange(len(source) - 1, len(u('lambda')), -1):
            prefix = source[:i]
            if u('lambda') not in prefix:
                return if_confused
            try:
                tree = ast.parse(prefix)
                source = prefix
                break
            except SyntaxError:
                continue
        else:
            return if_confused

    all_lambdas = extract_all_lambdas(tree)
    aligned_lambdas = [
        l for l in all_lambdas
        if args_for_lambda_ast(l) == args
    ]
    if len(aligned_lambdas) != 1:
        return if_confused
    lambda_ast = aligned_lambdas[0]
    assert lambda_ast.lineno == 1
    source = source[lambda_ast.col_offset:].strip()

    source = source[source.index(u('lambda')):]
    for i in hrange(len(source), len(u('lambda')), -1):  # pragma: no branch
        try:
            parsed = ast.parse(source[:i])
            assert len(parsed.body) == 1
            assert parsed.body
            if not isinstance(parsed.body[0].value, ast.Lambda):
                continue
            source = source[:i]
            break
        except SyntaxError:
            pass
    lines = source.split(u('\n'))
    lines = [PROBABLY_A_COMMENT.sub(u(''), l) for l in lines]
    source = u('\n').join(lines)

    source = WHITESPACE.sub(u(' '), source)
    source = SPACE_FOLLOWS_OPEN_BRACKET.sub(u('('), source)
    source = SPACE_PRECEDES_CLOSE_BRACKET.sub(u(')'), source)
    source = source.strip()
    return source


def get_pretty_function_description(f):
    if not hasattr(f, u('__name__')):
        return repr(f)
    name = f.__name__
    if name == u('<lambda>'):
        result = extract_lambda_source(f)
        return result
    elif isinstance(f, types.MethodType):
        self = f.__self__
        if not (self is None or inspect.isclass(self)):
            return u('%r.%s') % (self, name)
    return name


def nicerepr(v):
    if inspect.isfunction(v):
        return get_pretty_function_description(v)
    elif isinstance(v, type):
        return v.__name__
    else:
        return unicode_safe_repr(v)


def arg_string(f, args, kwargs):
    args, kwargs = convert_positional_arguments(f, args, kwargs)

    argspec = getargspec(f)

    bits = []

    for a in argspec.args:
        if a in kwargs:
            bits.append(u('%s=%s') % (a, nicerepr(kwargs.pop(a))))
    if kwargs:
        for a in sorted(kwargs):
            bits.append(u('%s=%s') % (a, nicerepr(kwargs[a])))

    return u(', ').join(
        [unicode_safe_repr(x) for x in args] +
        bits
    )


def unbind_method(f):
    """Take something that might be a method or a function and return the
    underlying function."""
    return getattr(f, u('im_func'), getattr(f, u('__func__'), f))


def check_valid_identifier(identifier):
    if not isidentifier(identifier):
        raise ValueError(u('%r is not a valid python identifier') %
                         (identifier,))


def eval_directory():
    return storage_directory('eval_source')


@contextmanager
def add_directory_to_path(d):
    sys.path.insert(0, d)
    yield
    sys.path.remove(d)


eval_cache = {}


def source_exec_as_module(source):
    try:
        return eval_cache[source]
    except KeyError:
        pass

    d = eval_directory()
    with add_directory_to_path(d):
        final_name = u('hypothesis_temporary_module_%s') % (
            hashlib.sha1(source.encode(u('utf-8'))).hexdigest(),
        )
        temporary_name = u('hypothesis_temporary_module_%s_%s') % (
            hashlib.sha1(source.encode(u('utf-8'))).hexdigest(),
            uuid.uuid4(),
        )
        temporary_filepath = os.path.join(d, temporary_name + u('.py'))
        final_filepath = os.path.join(d, final_name + u('.py'))
        f = open(temporary_filepath, u('w'))
        f.write(source)
        f.close()
        assert os.path.exists(temporary_filepath)

        try:
            os.rename(temporary_filepath, final_filepath)
        except OSError:  # pragma: no cover
            # The odds of final_filepath being a directory are basically zero,
            # and it's basically impossible for them to be on different
            # filesystems, so
            # if this is raised it's because the destination already exists on
            # Windows. That's fine, it won't be different, so just keep going,
            # deleting our tempfile.
            assert not os.path.isdir(final_filepath)
            os.remove(temporary_filepath)

        assert os.path.exists(final_filepath)
        assert not os.path.exists(temporary_filepath)
        with open(final_filepath) as r:
            assert r.read() == source
        importlib_invalidate_caches()
        result = __import__(final_name)
        eval_cache[source] = result
        return result


COPY_ARGSPEC_SCRIPT = """
from hypothesis.utils.conventions import not_set

def accept(%(funcname)s):
    def %(name)s(%(argspec)s):
        return %(funcname)s(%(invocation)s)
    return %(name)s
""".strip() + u('\n')


def copy_argspec(name, argspec):
    """A decorator which sets the name and argspec of the function passed into
    it."""
    check_valid_identifier(name)
    for a in argspec.args:
        check_valid_identifier(a)
    if argspec.varargs is not None:
        check_valid_identifier(argspec.varargs)
    if argspec.keywords is not None:
        check_valid_identifier(argspec.keywords)
    n_defaults = len(argspec.defaults or ())
    if n_defaults:
        parts = []
        for a in argspec.args[:-n_defaults]:
            parts.append(a)
        for a in argspec.args[-n_defaults:]:
            parts.append(u('%s=not_set') % (a,))
    else:
        parts = list(argspec.args)
    used_names = list(argspec.args)
    used_names.append(name)

    def accept(f):
        fargspec = getargspec(f)
        must_pass_as_kwargs = []
        invocation_parts = []
        for a in argspec.args:
            if a not in fargspec.args and not fargspec.varargs:
                must_pass_as_kwargs.append(a)
            else:
                invocation_parts.append(a)
        if argspec.varargs:
            used_names.append(argspec.varargs)
            parts.append(u('*') + argspec.varargs)
            invocation_parts.append(u('*') + argspec.varargs)
        for k in must_pass_as_kwargs:
            invocation_parts.append(u('%(k)s=%(k)s') % {u('k'): k})

        if argspec.keywords:
            used_names.append(argspec.keywords)
            parts.append(u('**') + argspec.keywords)
            invocation_parts.append(u('**') + argspec.keywords)

        candidate_names = [u('f')] + [
            u('f_%d') % (i,) for i in hrange(1, len(used_names) + 2)
        ]

        for funcname in candidate_names:  # pragma: no branch
            if funcname not in used_names:
                break

        base_accept = source_exec_as_module(
            COPY_ARGSPEC_SCRIPT % {
                u('name'): name,
                u('funcname'): funcname,
                u('argspec'): u(', ').join(parts),
                u('invocation'): u(', ').join(invocation_parts)
            }).accept

        result = base_accept(f)
        result.__defaults__ = argspec.defaults
        return result
    return accept


def impersonate(target):
    """Decorator to update the attributes of a function so that to external
    introspectors it will appear to be the target function.

    Note that this updates the function in place, it doesn't return a
    new one.

    """
    def accept(f):
        f.__code__ = update_code_location(
            f.__code__,
            target.__code__.co_filename, target.__code__.co_firstlineno
        )
        f.__name__ = target.__name__
        f.__module__ = target.__module__
        f.__doc__ = target.__doc__
        return f
    return accept


def proxies(target):
    def accept(proxy):
        return impersonate(target)(wraps(target)(
            copy_argspec(target.__name__, getargspec(target))(proxy)))
    return accept
