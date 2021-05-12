# encoding: utf-8

from ckan.types import Context
import json
import logging
from typing import Any, Iterable, List, Optional, Tuple, Union, overload

import ckan.common as converters
import sqlparse
import six

from six import string_types
from typing_extensions import Literal

from ckan.plugins.toolkit import get_action, ObjectNotFound, NotAuthorized

log = logging.getLogger(__name__)


def is_single_statement(sql: str):
    '''Returns True if received SQL string contains at most one statement'''
    return len(sqlparse.split(sql)) <= 1


def is_valid_field_name(name: str):
    '''
    Check that field name is valid:
    * can't start or end with whitespace characters
    * can't start with underscore
    * can't contain double quote (")
    * can't be empty
    '''
    return (name and name == name.strip() and
            not name.startswith('_') and
            '"' not in name)


def is_valid_table_name(name: str):
    if '%' in name:
        return False
    return is_valid_field_name(name)


def get_list(input: Any, strip_values: bool = True) -> Optional[List[str]]:
    '''Transforms a string or list to a list'''
    if input is None:
        return
    if input == '':
        return []

    converters_list = converters.aslist(input, ',', True)
    if strip_values:
        return [_strip(x) for x in converters_list]
    else:
        return converters_list


def validate_int(i: Any, non_negative: bool = False):
    try:
        i = int(i)
    except ValueError:
        return False
    return i >= 0 or not non_negative


def _strip(s: Any):
    if isinstance(s, string_types) and len(s) and s[0] == s[-1]:
        return s.strip().strip('"')
    return s


def should_fts_index_field_type(field_type: str):
    return field_type.lower() in ['tsvector', 'text', 'number']


def get_table_and_function_names_from_sql(context: Context, sql: str):
    '''Parses the output of EXPLAIN (FORMAT JSON) looking for table and
    function names

    It performs an EXPLAIN query against the provided SQL, and parses
    the output recusively.

    Note that this requires Postgres 9.x.

    :param context: a CKAN context dict. It must contain a 'connection' key
        with the current DB connection.
    :type context: dict
    :param sql: the SQL statement to parse for table and function names
    :type sql: string

    :rtype: a tuple with two list of strings, one for table and one for
    function names
    '''

    queries = [sql]
    table_names: List[str] = []
    function_names: List[str] = []

    while queries:
        sql = queries.pop()

        function_names.extend(_get_function_names_from_sql(sql))

        result = context['connection'].execute(
            'EXPLAIN (VERBOSE, FORMAT JSON) {0}'.format(
                six.ensure_str(sql))).fetchone()

        try:
            query_plan = json.loads(result['QUERY PLAN'])
            plan = query_plan[0]['Plan']

            t, q = _get_table_names_queries_from_plan(plan)
            table_names.extend(t)
            queries.extend(q)

        except ValueError:
            log.error('Could not parse query plan')
            raise

    return table_names, function_names


def _get_table_names_queries_from_plan(plan: Any):

    table_names: List[str] = []
    queries: List[str] = []

    if plan.get('Relation Name'):
        table_names.append(plan['Relation Name'])
    if 'Function Name' in plan and plan['Function Name'].startswith(
            'crosstab'):
        try:
            queries.append(_get_subquery_from_crosstab_call(
                plan['Function Call']))
        except ValueError:
            table_names.append('_unknown_crosstab_sql')

    if 'Plans' in plan:
        for child_plan in plan['Plans']:
            t, q = _get_table_names_queries_from_plan(child_plan)
            table_names.extend(t)
            queries.extend(q)

    return table_names, queries


def _get_function_names_from_sql(sql: str):
    function_names: List[str] = []

    def _get_function_names(tokens: Iterable[Any]):
        for token in tokens:
            if isinstance(token, sqlparse.sql.Function):
                function_name = token.get_name()
                if function_name not in function_names:
                    function_names.append(function_name)
            if hasattr(token, 'tokens'):
                _get_function_names(token.tokens)

    parsed = sqlparse.parse(sql)[0]
    _get_function_names(parsed.tokens)

    return function_names


def _get_subquery_from_crosstab_call(ct: str):
    """
    Crosstabs are a useful feature some sites choose to enable on
    their datastore databases. To support the sql parameter passed
    safely we accept only the simple crosstab(text) form where text
    is a literal SQL string, otherwise raise ValueError
    """
    if not ct.startswith("crosstab('") or not ct.endswith("'::text)"):
        raise ValueError('only simple crosstab calls supported')
    ct = ct[10:-8]
    if "'" in ct.replace("''", ""):
        raise ValueError('only escaped single quotes allowed in query')
    return ct.replace("''", "'")


def datastore_dictionary(resource_id: str):
    """
    Return the data dictionary info for a resource
    """
    try:
        return [
            f for f in get_action('datastore_search')(
                {}, {
                    u'resource_id': resource_id,
                    u'limit': 0,
                    u'include_total': False})['fields']
            if not f['id'].startswith(u'_')]
    except (ObjectNotFound, NotAuthorized):
        return []
