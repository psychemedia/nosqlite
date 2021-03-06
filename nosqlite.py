import json
import re
import sqlite3
import sys
import warnings

import pandas as pd
import collections

from functools import partial
from itertools import starmap

try:
    from itertools import ifilter as filter, imap as map
except ImportError:  # pragma: no cover Python >= 3.0
    pass


class MalformedQueryException(Exception):
    pass


class Connection(object):
    """
    The high-level connection to a sqlite database. Creating a connection accepts
    the same args and keyword args as the ``sqlite3.connect`` method
    """

    def __init__(self, *args, **kwargs):
        self._collections = {}
        self.connect(*args, **kwargs)

    def connect(self, *args, **kwargs):
        """
        Connect to a sqlite database only if no connection exists. Isolation level
        for the connection is automatically set to autocommit
        """
        self.db = sqlite3.connect(*args, **kwargs)
        self.db.isolation_level = None

    def close(self):
        """
        Terminate the connection to the sqlite database
        """
        if self.db is not None:
            self.db.close()

    def __getitem__(self, name):
        """
        A pymongo-like behavior for dynamically obtaining a collection of documents
        """
        if name not in self._collections:
            self._collections[name] = Collection(self.db, name)
        return self._collections[name]

    def __getattr__(self, name):
        if name in self.__dict__:
            return self.__dict__[name]
        return self[name]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_traceback):
        self.close()
        return False

    def drop_collection(self, name):
        """
        Drops a collection permanently if it exists
        """
        self.db.execute("drop table if exists %s" % name)


class Collection(object):
    """
    A virtual database table that holds JSON-type documents
    """

    def __init__(self, db, name, create=True):
        self.db = db
        self.name = name

        if create:
            self.create()

    def clear(self):
        """
        Clears all stored documents in this database. THERE IS NO GOING BACK
        """
        self.db.execute("delete from %s" % self.name)

    def exists(self):
        """
        Checks if this collection exists
        """
        return self._object_exists('table', self.name)

    def _object_exists(self, type, name):
        row = self.db.execute(
            "select count(1) from sqlite_master where type = ? and name = ?",
            (type, name.strip('[]'))
        ).fetchone()

        return int(row[0]) > 0

    def create(self):
        """
        Creates the collections database only if it does not already exist
        """
        self.db.execute("""
            create table if not exists %s (
                id integer primary key autoincrement,
                data text not null
            )
        """ % self.name)

    def insert(self, document):
        """
        Inserts a document into this collection. If a document already has an '_id'
        value it will be updated

        :returns: inserted document with id
        """
        if '_id' in document:
            return self.update(document)

        # Create it and return a modified one with the id
        cursor = self.db.execute("""
            insert into %s(data) values (?)
        """ % self.name, (json.dumps(document),))

        document['_id'] = cursor.lastrowid
        return document

    def update(self, document):
        """
        Updates a document stored in this collection. If the document does not
        already have an '_id' value, it will be created
        """
        if '_id' not in document:
            return self.insert(document)

        # Update the stored document, removing the id
        copy = document.copy()
        del copy['_id']

        self.db.execute("""
            update %s set data = ? where id = ?
        """ % self.name, (json.dumps(copy), document['_id']))

        return document

    def remove(self, document):
        """
        Removes a document from this collection. This will raise AssertionError if the
        document does not have an _id attribute
        """
        assert '_id' in document, 'Document must have an id'
        self.db.execute("delete from %s where id = ?" % self.name, (document['_id'],))

    def save(self, document):
        """
        Alias for ``update``
        """
        return self.update(document)

    def delete(self, document):
        """
        Alias for ``remove``
        """
        return self.remove(document)

    def _load(self, id, data):
        """
        Loads a JSON document taking care to apply the document id
        """
        if isinstance(data, bytes):  # pragma: no cover Python >= 3.0
            data = data.decode('utf-8')

        document = json.loads(data)
        document['_id'] = id
        return document

    def find(self, query=None, limit=None):
        """
        Returns a list of documents in this collection that match a given query
        """
        results = []
        query = query or {}

        # TODO: When indexes are implemented, we'll need to intelligently hit one of the
        # index stores so we don't do a full table scan
        cursor = self.db.execute("select id, data from %s" % self.name)
        apply = partial(self._apply_query, query)

        for match in filter(apply, starmap(self._load, cursor.fetchall())):
            results.append(match)

            # Just return if we already reached the limit
            if limit and len(results) == limit:
                return results

        return results



    #TH--
    def findd( self,q=None,proj=None,verbose=False,full=False ):
        ''' Piggbyack on self.find to offer deep search and projection limits on results '''
        #This should be implemented properly - see the FIX ME - this is just a stopgap
        
        q = q or {}
        proj = proj or {}
        
        #--- Deep search ----
        # So eg allow a search on {'foo.bar':'baz'}
        q1={}
        qn={}
        for i in q:
            if len(i.split('.'))==1:
                q1[i]=q[i]
            else: qn[i]=q[i]
        tmp=self.find(q1)
        tmp2=[]
        if len(qn)>0:
            for tx in tmp:
                #Check not level 1 queries
                test=False
                for i in qn:
                    try:
                        l=i.split('.')
                        tmpx=tx
                        for lx in l:
                            tmpx=tmpx[lx]
                        if tmpx==qn[i]:
                            test=True
                    except KeyError:
                        break
                    else:
                        if test: tmp2.append(tx)
        else:
            tmp2=tmp
    
        #------ Results projection ---
        # So eg on {'foo.bar':1} return items foo.bar=*, or foo.bar.* down...
        if proj=={}:
            results=tmp2
        else:
            results=[]
            for result in tmp2:
                projdata={}
                for p in proj:
                    if proj[p]==1:
                        #We'll use tmpproj to walk into projdata
                        tmpproj=projdata
                        tmpdict=result
                        levels=p.split('.')
                        path=[]
                        for level in levels:
                            path.append(level)
                            #Walk into the result
                            if level in tmpdict:
                                tmpdict=tmpdict[level]
                                if verbose:
                                    if level not in tmpproj:
                                        #Have we bottomed out in terms of finding atomic values yet?
                                        if isinstance(tmpdict, dict):
                                            if not full:
                                                tmpproj[level]={}
                                            elif len(path)<len(levels):
                                                tmpproj[level]={}
                                            else:
                                                tmpproj[level]=tmpdict.copy()
                                        else:
                                            tmpproj[level]=tmpdict
                                    #Move down to the next level of the results tree
                                    tmpproj=tmpproj[level]
                                if not isinstance(tmpdict, dict): projdata['_'.join(path)]=tmpdict
                            else:
                                break
                results.append(projdata)
        
        return results

    #pandas tables
    #http://stackoverflow.com/a/6027615/454773
    def _flatten(self, d, parent_key='', sep='_'):
        items = []
        for k, v in d.items():
            new_key = parent_key + sep + k if parent_key else k
            if isinstance(v, collections.MutableMapping):
                items.extend(self._flatten(v, new_key, sep=sep).items())
            else:
                items.append((new_key, v))
        return dict(items)
    #need an unflatten?
    
    def df(self):
        return pd.DataFrame( [self._flatten(x) for x in self.find()] )

    #--TH
    
    def _apply_query(self, query, document):
        """
        Applies a query to a document. Returns True if the document meets the criteria of
        the supplied query. The ``query`` argument generally follows mongodb style syntax
        and consists of the following logical checks and operators.

        Logical: $and, $or, $nor, $not
        Operators: $eq, $ne, $gt, $gte, $lt, $lte, $mod, $in, $nin, $all

        If no logical operator is supplied, it assumed that all field checks must pass. For
        example, these are equivalent:

            {'foo': 'bar', 'baz': 'qux'}
            {'$and': [{'foo': 'bar'}, {'baz': 'qux'}]}

        Both logical and operational queries can be nested in a complex fashion:

            {
                'bar': 'baz',
                '$or': [
                    {
                        'foo': {
                            '$gte': 0,
                            '$lte': 10,
                            '$mod': [2, 0]
                        }
                    },
                    {
                        'foo': {
                            '$gt': 10,
                            '$mod': [2, 1]
                        }
                    },
                ]
            }

        In the previous example, this will return any document where the 'bar' key is equal
        to 'baz' and either the 'foo' key is an even number between 0 and 10 or is an odd number
        greater than 10.
        """
        matches = []  # A list of booleans
        reapply = lambda q: self._apply_query(q, document)

        # FIXME: Fields need to support dot notation for sub-documents
        # i.e. {'foo.bar': 5} --> doc['foo']['bar'] == 5
        for field, value in query.items():
            # A more complex query type $and, $or, etc
            if field == '$and':
                matches.append(all(map(reapply, value)))
            elif field == '$or':
                matches.append(any(map(reapply, value)))
            elif field == '$nor':
                matches.append(not any(map(reapply, value)))
            elif field == '$not':
                matches.append(not self._apply_query(value, document))

            # Invoke a query operator
            elif isinstance(value, dict):
                for operator, arg in value.items():
                    if not self._get_operator_fn(operator)(field, arg, document):
                        matches.append(False)
                        break
                else:
                    matches.append(True)

            # Standard
            elif value != document.get(field, None):
                matches.append(False)

        return all(matches)

    def _get_operator_fn(self, op):
        """
        Returns the function in this module that corresponds to an operator string.
        This simply checks if there is a method that handles the operator defined
        in this module, replacing '$' with '_' (i.e. if this module has a _gt
        method for $gt) and returns it. If no match is found, or the operator does not
        start with '$', a MalformedQueryException is raised
        """
        if not op.startswith('$'):
            raise MalformedQueryException("Operator '%s' is not a valid query operation" % op)

        try:
            return getattr(sys.modules[__name__], op.replace('$', '_'))
        except AttributeError:
            raise MalformedQueryException("Operator '%s' is not currently implemented" % op)

    def find_one(self, query=None):
        """
        Equivalent to ``find(query, limit=1)[0]``
        """
        try:
            return self.find(query=query, limit=1)[0]
        except (sqlite3.OperationalError, IndexError):
            return None

    def find_and_modify(self, query=None, update=None):
        """
        Finds documents in this collection that match a given query and updates them
        """
        update = update or {}

        for document in self.find(query=query):
            document.update(update)
            self.update(document)

    def count(self, query=None):
        """
        Equivalent to ``len(find(query))``
        """
        return len(self.find(query=query))

    def rename(self, new_name):
        """
        Rename this collection
        """
        new_collection = Collection(self.db, new_name, create=False)
        assert not new_collection.exists()

        self.db.execute("alter table %s rename to %s" % (self.name, new_name))
        self.name = new_name

    def distinct(self, key):
        """
        Get a set of distinct values for the given key excluding an implicit
        None for documents that do not contain the key
        """
        return set(d[key] for d in filter(lambda d: key in d, self.find()))

    def create_index(self, key, reindex=True, sparse=False):
        """
        Creates an index if it does not exist then performs a full reindex for this collection
        """
        warnings.warn('Index support is currently very alpha and is not guaranteed')
        if isinstance(key, (list, tuple)):
            index_name = ','.join(key)
            index_columns = ', '.join('%s text' % f for f in key)
        else:
            index_name = key
            index_columns = '%s text' % key

        table_name = '[%s{%s}]' % (self.name, index_name)
        reindex = reindex or not self._object_exists('table', table_name)

        # Create a table store for the index data
        self.db.execute("""
            create table if not exists {table} (
                id integer primary key,
                {columns},
                foreign key(id) references {collection}(id) on delete cascade on update cascade
            )
        """.format(
            table=table_name,
            collection=self.name,
            columns=index_columns
        ))

        # Create the index
        self.db.execute("""
            create index if not exists [idx.{collection}{{index}}] on {table}({index})
        """.format(
            collection=self.name,
            index=index_name,
            table=table_name,
        ))

        if reindex:
            self.reindex(key)

    def ensure_index(self, key, sparse=False):
        """
        Equivalent to ``create_index(key, reindex=False)``
        """
        self.create_index(key, reindex=False, sparse=False)

    def reindex(self, table, sparse=False):
        warnings.warn('Index support is currently very alpha and is not guaranteed')
        index = re.findall(r'^\[.*\{(.*)\}\]$', table)[0].split(',')
        update = "update {table} set {key} = ? where id = ?"
        insert = "insert into {table}({index}) values({q})"
        count = "select count(1) from {table} where id = ?"
        qs = ('?,' * len(index)).rstrip(',')

        for document in self.find():
            # Ensure there's a row before we update
            row = self.db.execute(count.format(table=table), (document['_id'],)).fetchone()
            if int(row[0]) == 0:
                self.db.execute(insert.format(table=table, index=index, q=qs),
                                [None for x in index])

            for key in index:
                # Ignore this document if it doesn't have the key
                if key not in document and sparse:
                    continue

                self.db.execute(update.format(table=table, key=key),
                                (document.get(key, None), document['_id']))

    def drop_index(self):
        warnings.warn('Index support is currently very alpha and is not guaranteed')
        pass

    def drop_indexes(self):
        """
        Drop all indexes for this collection
        """
        warnings.warn('Index support is currently very alpha and is not guaranteed')
        pass


# BELOW ARE OPERATIONS FOR LOOKUPS
# TypeErrors are caught specifically for python 3 compatibility
def _eq(field, value, document):
    """
    Returns True if the value of a document field is equal to a given value
    """
    try:
        return document.get(field, None) == value
    except TypeError:  # pragma: no cover Python < 3.0
        return False


def _gt(field, value, document):
    """
    Returns True if the value of a document field is greater than a given value
    """
    try:
        return document.get(field, None) > value
    except TypeError:  # pragma: no cover Python < 3.0
        return False


def _lt(field, value, document):
    """
    Returns True if the value of a document field is less than a given value
    """
    try:
        return document.get(field, None) < value
    except TypeError:  # pragma: no cover Python < 3.0
        return False


def _gte(field, value, document):
    """
    Returns True if the value of a document field is greater than or
    equal to a given value
    """
    try:
        return document.get(field, None) >= value
    except TypeError:  # pragma: no cover Python < 3.0
        return False


def _lte(field, value, document):
    """
    Returns True if the value of a document field is less than or
    equal to a given value
    """
    try:
        return document.get(field, None) <= value
    except TypeError:  # pragma: no cover Python < 3.0
        return False


def _all(field, value, document):
    """
    Returns True if the value of document field contains all the values
    specified by ``value``. If supplied value is not an iterable, a
    MalformedQueryException is raised. If the value of the document field
    is not an iterable, False is returned
    """
    try:
        a = set(value)
    except TypeError:
        raise MalformedQueryException("'$all' must accept an iterable")

    try:
        b = set(document.get(field, []))
    except TypeError:
        return False
    else:
        return a.intersection(b) == a


def _in(field, value, document):
    """
    Returns True if document[field] is in the interable value. If the
    supplied value is not an iterable, then a MalformedQueryException is raised
    """
    try:
        values = iter(value)
    except TypeError:
        raise MalformedQueryException("'$in' must accept an iterable")

    return document.get(field, None) in values


def _ne(field, value, document):
    """
    Returns True if the value of document[field] is not equal to a given value
    """
    return document.get(field, None) != value


def _nin(field, value, document):
    """
    Returns True if document[field] is NOT in the interable value. If the
    supplied value is not an iterable, then a MalformedQueryException is raised
    """
    try:
        values = iter(value)
    except TypeError:
        raise MalformedQueryException("'$nin' must accept an iterable")

    return document.get(field, None) not in values


def _mod(field, value, document):
    """
    Performs a mod on a document field. Value must be a list or tuple with
    two values divisor and remainder (i.e. [2, 0]). This will essentially
    perform the following:

        document[field] % divisor == remainder

    If the value does not contain integers or is not a two-item list/tuple,
    a MalformedQueryException will be raised. If the value of document[field]
    cannot be converted to an integer, this will return False.
    """
    try:
        divisor, remainder = map(int, value)
    except (TypeError, ValueError):
        raise MalformedQueryException("'$mod' must accept an iterable: [divisor, remainder]")

    try:
        return int(document.get(field, None)) % divisor == remainder
    except (TypeError, ValueError):
        return False


def _exists(field, value, document):
    """
    Ensures a document has a given field or not. ``value`` must be either True or
    False, otherwise a MalformedQueryException is raised
    """
    if value not in (True, False):
        raise MalformedQueryException("'$exists' must be supplied a boolean")

    if value:
        return field in document
    else:
        return field not in document
