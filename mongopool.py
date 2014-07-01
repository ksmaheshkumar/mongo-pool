import re
import pymongo

from singleton import Singleton


class MongoPool(object):
    """
        Manages multiple mongo connections to different clusters, and performs
        database to connection matching.
    """

    __metaclass__ = Singleton

    def __init__(self, config=None, network_timeout=None):


        # Set timeout.
        self._network_timeout = network_timeout

        # { label -> cluster config } dict
        self._clusters = []
        self._parse_configs(config)
        self._mapped_databases = []

    def _parse_configs(self, config):
        """Builds a dict with information to connect to Clusters.

        Parses the list of configuration dictionaries passed by the user and
        builds an internal dict (_clusters) that holds information for creating
        Clients connecting to Clusters and matching database names.

        Args:
            config: A list of dictionaries containing connecting and
                identification information about Clusters.
                A dict has the following structure:
                {label: {host, port, read_preference, replicaSet, dbpath}}.

        Raises:
            Exception('No configurations provided'): no configuration provided.
        """
        if config is None:
            raise Exception('No configurations provided')

        for config_dict in config:
            label = config_dict.keys()[0]
            cfg = config_dict[label]
            # Transform dbpath to something digestable by regexp.
            dbpath = cfg['dbpath']
            pattern = self._parse_dbpath(dbpath)

            read_preference = cfg.get('read_preference', 'primary').upper()
            read_preference = self._get_read_preference(read_preference)

            # Put all parameters that could be passed to pymongo.MongoClient
            # in a separate dict, to ease MongoClient creation.
            cluster_config = {
                'params': {
                    'host': cfg['host'],
                    'port': cfg['port'],
                    'read_preference': read_preference,
                },
                'pattern': pattern,
                'label': label
            }

            replicaSet = cfg.get('replicaSet')
            if replicaSet:
                cluster_config['params']['replicaSet'] = replicaSet
                cluster_config = self._convert_for_replica_set(cluster_config)

            self._clusters.append(cluster_config)

    def _parse_dbpath(self, dbpath):
        """Converts the dbpath to a regexp pattern.

        Transforms dbpath from a string or an array of strings to a
        regexp pattern which will be used to match database names.

        Args:
            dbpath: a string or an array containing the databases to be matched
                from a cluster.

        Returns:
            A regexp pattern that will match any of the desired databases on
            on a cluster.
        """
        if isinstance(dbpath, list):
            # Support dbpath param as an array.
            dbpath = '|'.join(dbpath)

        # Append $ (end of string) so that twit will not match twitter!
        if not dbpath.endswith('$'):
            dbpath = '(%s)$' % dbpath

        return dbpath

    def _get_read_preference(self, read_preference):
        """Converts read_preference from string to pymongo.ReadPreference value.

            Args:
                read_preference: string containig the read_preference from the
                    config file
            Returns:
                A value from the pymongo.ReadPreference enum

            Raises:
                Exception: Invalid read preference"""
        read_preference = getattr(pymongo.ReadPreference, read_preference, None)
        if read_preference is None:
            raise ValueError('Invalid read preference: %s' % read_preference)
        return read_preference

    def _convert_for_replica_set(self, cluster_config):
        """Converts the cluster config to be used with MongoReplicaSetClient.

        MongoReplicaSetClient has a slightly different API from MongoClient.
        Changes host field into hosts_or_uri and moves port into the
        hosts_or_uri field.

        Args:
            cluster_config: Dictinary containing parameters for a MongoClient.

        Returns:
            A dict containing parameters for creating a MongoReplicaSetClient.
        """

        host = cluster_config['params'].pop('host')
        port = cluster_config['params'].pop('port')

        if not isinstance(host, list):
            host = [host]
        hosts_or_uri = ','.join(['%s:%s' % (h, port) for h in host])

        cluster_config['params']['hosts_or_uri'] = hosts_or_uri
        return cluster_config

    def set_timeout(self, network_timeout):
        """Set the timeout for existing and future Clients.

        Close all current connections. This will cause future operations to
        create new Clients with the network_timeout passed through
        socketTimeoutMS optional parameter.

        Args:
            network_timeout: The new value in milliseconds for the timeout.
        """
        # Do nothing if attempting to set the same timeout twice.
        if network_timeout == self._network_timeout:
            return
        self._network_timeout = network_timeout
        self._disconnect()

    def _disconnect(self):
        """Disconnect from all MongoDB Clients."""
        for cluster in self._clusters:
            if 'connection' in cluster:
                c = cluster.pop('connection')
                c.close()
        # Remove all attributes that are database names so that next time
        # when they are accessed, __getattr__ will be called and will create
        # new Clients
        for dbname in self._mapped_databases:
            self.__delattr__(dbname)
        self._mapped_databases = []

    def _get_connection(self, cluster):
        """Return a connection to a Cluster.

        Return a MongoClient or a MongoReplicaSetClient for the given Cluster.
        This is done in a lazy manner (if there is already a Client connected to
        the Cluster, it is returned and no other Client is created).

        Args:
            cluster: A dict containing information about a cluster.

        Returns:
            A MongoClient or MongoReplicaSetClient instance connected to the
            desired cluster
        """
        if 'connection' not in cluster:
            if 'replicaSet' in cluster['params']:
                cluster['connection'] = pymongo.MongoReplicaSetClient(
                    socketTimeoutMS=self._network_timeout,
                    safe=True,
                    **cluster['params'])
            else:
                cluster['connection'] = pymongo.MongoClient(
                    socketTimeoutMS=self._network_timeout,
                    safe=True,
                    **cluster['params'])

        return cluster['connection']

    def _match_dbname(self, dbname):
        """Map a database name to the Cluster that holds the database.

        Args:
            dbname: A database name.

        Returns:
            A dict containing the information about the Cluster that holds the
            database.
        """
        for config in self._clusters:
            if re.match(config['pattern'], dbname):
                return config
        else:
            raise Exception('No such database %s.' % dbname)

    def __getattr__(self, name):
        """Map a database name to the coresponding pymongo.Database instance"""
        config = self._match_dbname(name)
        connection = self._get_connection(config)
        db_name = name
        database = connection[db_name]
        # Remember this name->database mapping on the object. This will cause
        # future references to the same name to NOT invoke self.__getattr__,
        # and be resolved directly at object level.
        setattr(self, name, database)
        self._mapped_databases.append(name)
        return database

    def __getitem__(self, key):
        return getattr(self, key)