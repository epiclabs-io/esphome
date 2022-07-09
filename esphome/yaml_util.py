from copy import deepcopy
import fnmatch
import functools
import inspect
import math
import os

import uuid
import yaml
import yaml.constructor

from jinja2.exceptions import TemplateError, TemplateSyntaxError, UndefinedError

from esphome import core, git
from esphome.config_helpers import read_config_file
from esphome.const import (
    CONF_FILE,
    CONF_ID,
    CONF_PASSWORD,
    CONF_REF,
    CONF_REFRESH,
    CONF_SUBSTITUTIONS,
    CONF_URL,
    CONF_USERNAME,
    CONF_VARS,
)
from esphome.core import (
    EsphomeError,
    IPAddress,
    Lambda,
    MACAddress,
    TimePeriod,
)
from esphome.database import ESPHomeDataBase, make_data_base
from esphome.helpers import add_class_to_obj
from esphome.jinja import expand_str, validate_vars
from esphome.util import OrderedDict, filter_yaml_files
import esphome.config_validation as cv

# Mostly copied from Home Assistant because that code works fine and
# let's not reinvent the wheel here

SECRET_YAML = "secrets.yaml"
_SECRET_CACHE = {}
_SECRET_VALUES = {}


class ForList(list):
    pass


INCLUDE_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_FILE): cv.string,
        cv.Optional(CONF_URL): cv.url,
        cv.Optional(CONF_USERNAME): cv.string,
        cv.Optional(CONF_PASSWORD): cv.string,
        cv.Optional(CONF_REF): cv.git_ref,
        cv.Optional(CONF_REFRESH, default="1d"): cv.All(cv.string, cv.source_refresh),
        cv.Optional(CONF_VARS): validate_vars,
    }
)


class ESPForceValue:
    pass


def _add_data_ref(fn):
    @functools.wraps(fn)
    def wrapped(loader, node):
        res = fn(loader, node)
        # newer PyYAML versions use generators, resolve them
        if inspect.isgenerator(res):
            generator = res
            res = next(generator)
            # Let generator finish
            for _ in generator:
                pass
        res = make_data_base(res)
        if isinstance(res, ESPHomeDataBase):
            res.from_node(node)
        return res

    return wrapped


class ESPHomeLoader(
    yaml.SafeLoader
):  # pylint: disable=too-many-ancestors,too-many-public-methods
    """Loader class that keeps track of line numbers."""

    def __init__(self, content, vars=None, disable_str_expansion=False):
        if vars is None:
            vars = {}
        self.vars = vars
        self.disable_str_expansion = disable_str_expansion
        yaml.SafeLoader.__init__(self, content)

    @_add_data_ref
    def construct_yaml_int(self, node):
        return super().construct_yaml_int(node)

    @_add_data_ref
    def construct_yaml_float(self, node):
        return super().construct_yaml_float(node)

    @_add_data_ref
    def construct_yaml_binary(self, node):
        return super().construct_yaml_binary(node)

    @_add_data_ref
    def construct_yaml_omap(self, node):
        return super().construct_yaml_omap(node)

    @_add_data_ref
    def construct_yaml_str(self, node):
        st = super().construct_yaml_str(node)
        if self.disable_str_expansion:
            return st
        try:
            result = expand_str(st, self.vars)
        except UndefinedError as err:
            raise yaml.MarkedYAMLError(
                f"Variable is undefined: {err.message}",
                node.start_mark,
            )

        except TemplateSyntaxError as err:
            raise yaml.MarkedYAMLError(
                f"Error in line {err.lineno} of jinja expression: {err.message}",
                node.start_mark,
            )
        except TemplateError as err:
            raise yaml.MarkedYAMLError(
                f"Error in jinja expression: {err.message}",
                node.start_mark,
            )
        except Exception as err:
            raise yaml.MarkedYAMLError(
                f"Error in jinja expression: {err}",
                node.start_mark,
            )

        if result and result != st:
            result = make_data_base(result)
            if hasattr(node, "from_node"):
                result.from_node(node)

        return result

    @_add_data_ref
    def construct_sequence(self, node, deep=False):
        def flatten_for(items):  # Flatten the output of "!for"
            result = []
            for item in items:
                if isinstance(item, ForList):
                    result += flatten_for(item)
                elif item is not None:
                    result.append(item)
            return result

        return flatten_for(super().construct_sequence(node, deep=deep))

    @_add_data_ref
    def construct_yaml_seq(self, node):
        return super().construct_yaml_seq(node)

    @_add_data_ref
    def construct_yaml_map(self, node):
        """Traverses the given mapping node and returns a list of constructed key-value pairs."""
        assert isinstance(node, yaml.MappingNode)
        # A list of key-value pairs we find in the current mapping
        pairs = []
        # A list of key-value pairs we find while resolving merges ('<<' key), will be
        # added to pairs in a second pass
        merge_pairs = []
        # A dict of seen keys so far, used to alert the user of duplicate keys and checking
        # which keys to merge.
        # Value of dict items is the start mark of the previous declaration.
        seen_keys = {}

        for key_node, value_node in node.value:
            # merge key is '<<'
            is_merge_key = key_node.tag == "tag:yaml.org,2002:merge"
            # key has no explicit tag set
            is_default_tag = key_node.tag == "tag:yaml.org,2002:value"

            if is_default_tag:
                # Default tag for mapping keys is string
                key_node.tag = "tag:yaml.org,2002:str"

            if not is_merge_key:
                # base case, this is a simple key-value pair

                # disable jinja in keys:
                old_disable = self.disable_str_expansion
                self.disable_str_expansion = True

                key = self.construct_object(key_node)
                self.disable_str_expansion = old_disable

                value = self.construct_object(value_node)

                # Check if key is hashable
                try:
                    hash(key)
                except TypeError:
                    # pylint: disable=raise-missing-from
                    raise yaml.constructor.ConstructorError(
                        f'Invalid key "{key}" (not hashable)', key_node.start_mark
                    )

                key = make_data_base(str(key))
                key.from_node(key_node)

                # Check if it is a duplicate key
                if key in seen_keys:
                    raise yaml.constructor.ConstructorError(
                        f'Duplicate key "{key}"',
                        key_node.start_mark,
                        "NOTE: Previous declaration here:",
                        seen_keys[key],
                    )
                seen_keys[key] = key_node.start_mark

                # Add to pairs
                pairs.append((key, value))
                continue

            # This is a merge key, resolve value and add to merge_pairs
            value = self.construct_object(value_node)
            if isinstance(value, dict):
                # base case, copy directly to merge_pairs
                # direct merge, like "<<: {some_key: some_value}"
                merge_pairs.extend(value.items())
            elif isinstance(value, list):
                # sequence merge, like "<<: [{some_key: some_value}, {other_key: some_value}]"
                for item in value:
                    if not isinstance(item, dict):
                        raise yaml.constructor.ConstructorError(
                            "While constructing a mapping",
                            node.start_mark,
                            f"Expected a mapping for merging, but found {type(item)}",
                            value_node.start_mark,
                        )
                    merge_pairs.extend(item.items())
            else:
                raise yaml.constructor.ConstructorError(
                    "While constructing a mapping",
                    node.start_mark,
                    f"Expected a mapping or list of mappings for merging, but found {type(value)}",
                    value_node.start_mark,
                )

        if merge_pairs:
            # We found some merge keys along the way, merge them into base pairs
            # https://yaml.org/type/merge.html
            # Construct a new merge set with values overridden by current mapping or earlier
            # sequence entries removed
            for key, value in merge_pairs:
                if key in seen_keys:
                    # key already in the current map or from an earlier merge sequence entry,
                    # do not override
                    #
                    # "... each of its key/value pairs is inserted into the current mapping,
                    # unless the key already exists in it."
                    #
                    # "If the value associated with the merge key is a sequence, then this sequence
                    #  is expected to contain mapping nodes and each of these nodes is merged in
                    #  turn according to its order in the sequence. Keys in mapping nodes earlier
                    #  in the sequence override keys specified in later mapping nodes."
                    continue
                pairs.append((key, value))
                # Add key node to seen keys, for sequence merge values.
                seen_keys[key] = None

        return OrderedDict(pairs)

    @_add_data_ref
    def construct_env_var(self, node):
        args = node.value.split()
        # Check for a default value
        if len(args) > 1:
            return os.getenv(args[0], " ".join(args[1:]))
        if args[0] in os.environ:
            return os.environ[args[0]]
        raise yaml.MarkedYAMLError(
            f"Environment variable '{node.value}' not defined", node.start_mark
        )

    @property
    def _directory(self):
        return os.path.dirname(self.name)

    def _rel_path(self, *args):
        return os.path.join(self._directory, *args)

    @_add_data_ref
    def construct_secret(self, node):
        secrets = _load_yaml_internal(self._rel_path(SECRET_YAML), self.vars.copy())
        if node.value not in secrets:
            raise yaml.MarkedYAMLError(
                f"Secret '{node.value}' not defined", node.start_mark
            )
        val = secrets[node.value]
        _SECRET_VALUES[str(val)] = node.value
        return val

    @_add_data_ref
    def construct_include(self, node):
        def extract_file_vars(node):
            fields = INCLUDE_SCHEMA(self.construct_yaml_map(node))
            file = fields.get(CONF_FILE)
            url = fields.get(CONF_URL)
            if file is None:
                raise yaml.MarkedYAMLError("Must include 'file'", node.start_mark)
            vars = fields.get(CONF_VARS) or {}
            if url is not None:
                repo_dir = git.clone_or_update(
                    url=url,
                    ref=fields.get(CONF_REF),
                    refresh=fields[CONF_REFRESH],
                    domain="includes",
                    username=fields.get(CONF_USERNAME),
                    password=fields.get(CONF_PASSWORD),
                )
                path = repo_dir / file
            else:
                path = self._rel_path(file)

            return path, validate_vars(vars)

        if isinstance(node, yaml.nodes.MappingNode):
            path, vars = extract_file_vars(node)
        else:
            file, vars = node.value, {}
            path = self._rel_path(file)

        return _load_yaml_internal(path, {**self.vars, **vars})

    @_add_data_ref
    def construct_literal(self, node):
        # restore tag:
        if isinstance(node, yaml.ScalarNode):
            node.tag = self.DEFAULT_SCALAR_TAG
        elif isinstance(node, yaml.SequenceNode):
            node.tag = self.DEFAULT_SEQUENCE_TAG
        elif isinstance(node, yaml.MappingNode):
            node.tag = self.DEFAULT_MAPPING_TAG
        else:
            raise yaml.MarkedYAMLError(f"Unknown node type {type(node)}")

        # !literal disables jinja:
        old_disable = self.disable_str_expansion
        self.disable_str_expansion = True

        # construct the object as if !literal was not present:
        result = self.construct_object(deepcopy(node))

        # leave jinja string expansion as it was:
        self.disable_str_expansion = old_disable
        return result

    @_add_data_ref
    def construct_for(self, node):
        if self.disable_str_expansion:
            return None

        items = None
        varname = "item"
        repeat = None
        for key_node, value_node in node.value:
            key = self.construct_object(key_node)
            if key == "items":
                items = self.construct_object(value_node)
            if key == "var":
                varname = self.construct_object(value_node)
            if key == "repeat":
                repeat = value_node

        if isinstance(items, str):
            items = self.vars[items]

        if not isinstance(items, list):
            raise yaml.MarkedYAMLError(
                "items must be a list",
                node.start_mark,
            )
        if not isinstance(varname, str):
            raise yaml.MarkedYAMLError(
                "var must be a string",
                node.start_mark,
            )
        if repeat is None:
            raise yaml.MarkedYAMLError(
                "missing repeat value",
                node.start_mark,
            )

        result = []
        oldvars = self.vars
        for i in items:
            vars = self.vars = oldvars.copy()
            vars[varname] = i
            obj = make_data_base(self.construct_object(deepcopy(repeat)))
            if hasattr(obj, "from_node"):
                obj.from_node(node)
            result.append(obj)

        self.vars = oldvars
        return ForList(result)

    @_add_data_ref
    def construct_if(self, node):
        if self.disable_str_expansion:
            return None

        condition = None
        then_node = None
        else_node = None
        for key_node, value_node in node.value:
            key = self.construct_object(key_node)
            if key == "condition":
                condition = self.construct_object(value_node)
            if key == "then":
                then_node = value_node
            if key == "else":
                else_node = value_node

        if then_node is None:
            raise yaml.MarkedYAMLError(
                "missing then value",
                node.start_mark,
            )

        if condition:
            return self.construct_object(then_node)

        if else_node is not None:
            return self.construct_object(else_node)

        return None

    @_add_data_ref
    def construct_merge(self, node):
        if self.disable_str_expansion:
            return None

        def merge(old, new):
            # pylint: disable=no-else-return
            if isinstance(new, dict):
                if not isinstance(old, dict):
                    return new
                res = old.copy()
                for k, v in new.items():
                    res[k] = merge(old[k], v) if k in old else v
                return res
            elif isinstance(new, list):
                if not isinstance(old, list):
                    return new
                index = OrderedDict()
                pos = 0
                for item in new:
                    if isinstance(item, dict) and CONF_ID in item:
                        index[str(item[CONF_ID])] = item
                    else:
                        index[pos] = item
                        pos += 1

                merged_old = []
                for item in old:
                    if isinstance(item, dict) and CONF_ID in item:
                        id = str(item[CONF_ID])
                        if id in index:
                            new_item = index[id]
                            item = merge(item, new_item)
                            del index[id]
                    merged_old.append(item)

                return merged_old + list(index.values())
            elif new is None:
                return old

            return new

        if not isinstance(node, yaml.SequenceNode):
            raise yaml.MarkedYAMLError(
                "!merge expects a list",
                node.start_mark,
            )
        node = deepcopy(node)
        node.tag = self.DEFAULT_SEQUENCE_TAG
        mergelist = self.construct_object(node)
        value = None
        for obj in mergelist:
            value = merge(value, obj)
        print(value)
        return value

    @_add_data_ref
    def construct_include_dir_list(self, node):
        files = filter_yaml_files(_find_files(self._rel_path(node.value), "*.yaml"))
        return [_load_yaml_internal(f, self.vars.copy()) for f in files]

    @_add_data_ref
    def construct_include_dir_merge_list(self, node):
        files = filter_yaml_files(_find_files(self._rel_path(node.value), "*.yaml"))
        merged_list = []
        for fname in files:
            loaded_yaml = _load_yaml_internal(fname, self.vars.copy())
            if isinstance(loaded_yaml, list):
                merged_list.extend(loaded_yaml)
        return merged_list

    @_add_data_ref
    def construct_include_dir_named(self, node):
        files = filter_yaml_files(_find_files(self._rel_path(node.value), "*.yaml"))
        mapping = OrderedDict()
        for fname in files:
            filename = os.path.splitext(os.path.basename(fname))[0]
            mapping[filename] = _load_yaml_internal(fname, self.vars.copy())
        return mapping

    @_add_data_ref
    def construct_include_dir_merge_named(self, node):
        files = filter_yaml_files(_find_files(self._rel_path(node.value), "*.yaml"))
        mapping = OrderedDict()
        for fname in files:
            loaded_yaml = _load_yaml_internal(fname, self.vars.copy())
            if isinstance(loaded_yaml, dict):
                mapping.update(loaded_yaml)
        return mapping

    @_add_data_ref
    def construct_lambda(self, node):
        if not isinstance(node, yaml.ScalarNode) or not isinstance(node.value, str):
            raise yaml.MarkedYAMLError("!lambda must tag a string")
        # replace lambda tag:
        node.tag = self.DEFAULT_SCALAR_TAG
        code = self.construct_object(deepcopy(node))
        return Lambda(str(code))

    @_add_data_ref
    def construct_force(self, node):
        obj = self.construct_scalar(node)
        return add_class_to_obj(obj, ESPForceValue)


ESPHomeLoader.add_constructor("tag:yaml.org,2002:int", ESPHomeLoader.construct_yaml_int)
ESPHomeLoader.add_constructor(
    "tag:yaml.org,2002:float", ESPHomeLoader.construct_yaml_float
)
ESPHomeLoader.add_constructor(
    "tag:yaml.org,2002:binary", ESPHomeLoader.construct_yaml_binary
)
ESPHomeLoader.add_constructor(
    "tag:yaml.org,2002:omap", ESPHomeLoader.construct_yaml_omap
)
ESPHomeLoader.add_constructor("tag:yaml.org,2002:str", ESPHomeLoader.construct_yaml_str)
ESPHomeLoader.add_constructor("tag:yaml.org,2002:seq", ESPHomeLoader.construct_yaml_seq)
ESPHomeLoader.add_constructor("tag:yaml.org,2002:map", ESPHomeLoader.construct_yaml_map)
ESPHomeLoader.add_constructor("!env_var", ESPHomeLoader.construct_env_var)
ESPHomeLoader.add_constructor("!secret", ESPHomeLoader.construct_secret)
ESPHomeLoader.add_constructor("!include", ESPHomeLoader.construct_include)
ESPHomeLoader.add_constructor("!literal", ESPHomeLoader.construct_literal)
ESPHomeLoader.add_constructor("!for", ESPHomeLoader.construct_for)
ESPHomeLoader.add_constructor("!if", ESPHomeLoader.construct_if)
ESPHomeLoader.add_constructor("!merge", ESPHomeLoader.construct_merge)

ESPHomeLoader.add_constructor(
    "!include_dir_list", ESPHomeLoader.construct_include_dir_list
)
ESPHomeLoader.add_constructor(
    "!include_dir_merge_list", ESPHomeLoader.construct_include_dir_merge_list
)
ESPHomeLoader.add_constructor(
    "!include_dir_named", ESPHomeLoader.construct_include_dir_named
)
ESPHomeLoader.add_constructor(
    "!include_dir_merge_named", ESPHomeLoader.construct_include_dir_merge_named
)
ESPHomeLoader.add_constructor("!lambda", ESPHomeLoader.construct_lambda)
ESPHomeLoader.add_constructor("!force", ESPHomeLoader.construct_force)


def load_vars(fname: str, override_vars: dict = None):
    """Preloads a config file, extracts substitutions and resolves
    command-line substitutions"""

    if override_vars is None:
        override_vars = {}

    # parse override_vars as YAML:
    override_vars = {
        key: _load_yaml_string(value, f"command line variable '{key}'", None, True)
        for key, value in override_vars.items()
    }

    raw_config = _load_yaml_internal(fname, None, True)
    if CONF_SUBSTITUTIONS in raw_config:
        substitutions = raw_config[CONF_SUBSTITUTIONS]
    else:
        substitutions = {}

    with cv.prepend_path("substitutions"):
        if not isinstance(substitutions, dict):
            raise cv.Invalid(
                f"Substitutions must be a key to value mapping, got {type(substitutions)}"
            )

        substitutions = validate_vars(substitutions)

        # override file substitutions with incoming ones (i.e. command-line)
        substitutions = {
            **substitutions,
            **override_vars,
        }

        vars = {}
        for (skey, svalue) in substitutions.items():
            with cv.prepend_path(skey):
                if isinstance(svalue, str):
                    try:
                        svalue = expand_str(svalue, vars)
                    except (TemplateError, TemplateSyntaxError) as err:
                        raise cv.Invalid(
                            f"Error in substitution with name { skey }: {err.message}"
                        )
                    except Exception as err:
                        raise cv.Invalid(
                            f"Error in substitution with name { skey }: {err}"
                        )
                vars[skey] = svalue

    return vars


def load_yaml(fname, clear_secrets=True, vars=None):
    if vars is None:
        vars = {}

    if clear_secrets:
        _SECRET_VALUES.clear()
        _SECRET_CACHE.clear()

    config = _load_yaml_internal(fname, vars)
    if CONF_SUBSTITUTIONS in config:
        del config[CONF_SUBSTITUTIONS]
    return config


def _load_yaml_string(content, name, vars=None, disable_str_expansion=False):
    if vars is None:
        vars = {}

    loader = ESPHomeLoader(content, vars, disable_str_expansion)
    loader.name = name
    try:
        return loader.get_single_data() or OrderedDict()
    except yaml.YAMLError as exc:
        raise EsphomeError(exc) from exc
    finally:
        loader.dispose()


def _load_yaml_internal(fname, vars, disable_str_expansion=False):
    content = read_config_file(fname)
    return _load_yaml_string(content, fname, vars, disable_str_expansion)


def dump(dict_):
    """Dump YAML to a string and remove null."""
    return yaml.dump(
        dict_, default_flow_style=False, allow_unicode=True, Dumper=ESPHomeDumper
    )


def _is_file_valid(name):
    """Decide if a file is valid."""
    return not name.startswith(".")


def _find_files(directory, pattern):
    """Recursively load files in a directory."""
    for root, dirs, files in os.walk(directory, topdown=True):
        dirs[:] = [d for d in dirs if _is_file_valid(d)]
        for basename in files:
            if _is_file_valid(basename) and fnmatch.fnmatch(basename, pattern):
                filename = os.path.join(root, basename)
                yield filename


def is_secret(value):
    try:
        return _SECRET_VALUES[str(value)]
    except (KeyError, ValueError):
        return None


class ESPHomeDumper(yaml.SafeDumper):  # pylint: disable=too-many-ancestors
    def represent_mapping(self, tag, mapping, flow_style=None):
        value = []
        node = yaml.MappingNode(tag, value, flow_style=flow_style)
        if self.alias_key is not None:
            self.represented_objects[self.alias_key] = node
        best_style = True
        if hasattr(mapping, "items"):
            mapping = list(mapping.items())
        for item_key, item_value in mapping:
            node_key = self.represent_data(item_key)
            node_value = self.represent_data(item_value)
            if not (isinstance(node_key, yaml.ScalarNode) and not node_key.style):
                best_style = False
            if not (isinstance(node_value, yaml.ScalarNode) and not node_value.style):
                best_style = False
            value.append((node_key, node_value))
        if flow_style is None:
            if self.default_flow_style is not None:
                node.flow_style = self.default_flow_style
            else:
                node.flow_style = best_style
        return node

    def represent_secret(self, value):
        return self.represent_scalar(tag="!secret", value=_SECRET_VALUES[str(value)])

    def represent_stringify(self, value):
        if is_secret(value):
            return self.represent_secret(value)
        return self.represent_scalar(tag="tag:yaml.org,2002:str", value=str(value))

    # pylint: disable=arguments-renamed
    def represent_bool(self, value):
        return self.represent_scalar(
            "tag:yaml.org,2002:bool", "true" if value else "false"
        )

    # pylint: disable=arguments-renamed
    def represent_int(self, value):
        if is_secret(value):
            return self.represent_secret(value)
        return self.represent_scalar(tag="tag:yaml.org,2002:int", value=str(value))

    # pylint: disable=arguments-renamed
    def represent_float(self, value):
        if is_secret(value):
            return self.represent_secret(value)
        if math.isnan(value):
            value = ".nan"
        elif math.isinf(value):
            value = ".inf" if value > 0 else "-.inf"
        else:
            value = str(repr(value)).lower()
            # Note that in some cases `repr(data)` represents a float number
            # without the decimal parts.  For instance:
            #   >>> repr(1e17)
            #   '1e17'
            # Unfortunately, this is not a valid float representation according
            # to the definition of the `!!float` tag.  We fix this by adding
            # '.0' before the 'e' symbol.
            if "." not in value and "e" in value:
                value = value.replace("e", ".0e", 1)
        return self.represent_scalar(tag="tag:yaml.org,2002:float", value=value)

    def represent_lambda(self, value):
        if is_secret(value.value):
            return self.represent_secret(value.value)
        return self.represent_scalar(tag="!lambda", value=value.value, style="|")

    def represent_id(self, value):
        if is_secret(value.id):
            return self.represent_secret(value.id)
        return self.represent_stringify(value.id)


ESPHomeDumper.add_multi_representer(
    dict, lambda dumper, value: dumper.represent_mapping("tag:yaml.org,2002:map", value)
)
ESPHomeDumper.add_multi_representer(
    list,
    lambda dumper, value: dumper.represent_sequence("tag:yaml.org,2002:seq", value),
)
ESPHomeDumper.add_multi_representer(bool, ESPHomeDumper.represent_bool)
ESPHomeDumper.add_multi_representer(str, ESPHomeDumper.represent_stringify)
ESPHomeDumper.add_multi_representer(int, ESPHomeDumper.represent_int)
ESPHomeDumper.add_multi_representer(float, ESPHomeDumper.represent_float)
ESPHomeDumper.add_multi_representer(IPAddress, ESPHomeDumper.represent_stringify)
ESPHomeDumper.add_multi_representer(MACAddress, ESPHomeDumper.represent_stringify)
ESPHomeDumper.add_multi_representer(TimePeriod, ESPHomeDumper.represent_stringify)
ESPHomeDumper.add_multi_representer(Lambda, ESPHomeDumper.represent_lambda)
ESPHomeDumper.add_multi_representer(core.ID, ESPHomeDumper.represent_id)
ESPHomeDumper.add_multi_representer(uuid.UUID, ESPHomeDumper.represent_stringify)
