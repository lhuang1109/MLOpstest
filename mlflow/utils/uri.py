import pathlib
import posixpath
import urllib.parse
import uuid
from typing import Tuple, Any

from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import INVALID_PARAMETER_VALUE
from mlflow.store.db.db_types import DATABASE_ENGINES
from mlflow.utils.validation import _validate_db_type_string
from mlflow.utils.os import is_windows

_INVALID_DB_URI_MSG = (
    "Please refer to https://mlflow.org/docs/latest/tracking.html#storage for "
    "format specifications."
)

_DBFS_FUSE_PREFIX = "/dbfs/"
_DBFS_HDFS_URI_PREFIX = "dbfs:/"
_DATABRICKS_UNITY_CATALOG_SCHEME = "databricks-uc"


def is_local_uri(uri, is_tracking_or_registry_uri=True):
    """
    Returns true if the specified URI is a local file path (/foo or file:/foo).

    :param uri: The URI.
    :param is_tracking_uri: Whether or not the specified URI is an MLflow Tracking or MLflow
                            Model Registry URI. Examples of other URIs are MLflow artifact URIs,
                            filesystem paths, etc.
    """
    if uri == "databricks" and is_tracking_or_registry_uri:
        return False

    if is_windows() and uri.startswith("\\\\"):
        # windows network drive path looks like: "\\<server name>\path\..."
        return False

    parsed_uri = urllib.parse.urlparse(uri)
    if parsed_uri.hostname and not (
        parsed_uri.hostname == "."
        or parsed_uri.hostname.startswith("localhost")
        or parsed_uri.hostname.startswith("127.0.0.1")
    ):
        return False

    scheme = parsed_uri.scheme
    if scheme == "" or scheme == "file":
        return True

    if is_windows() and len(scheme) == 1 and scheme.lower() == pathlib.Path(uri).drive.lower()[0]:
        return True

    return False


def is_file_uri(uri):
    return urllib.parse.urlparse(uri).scheme == "file"


def is_http_uri(uri):
    scheme = urllib.parse.urlparse(uri).scheme
    return scheme == "http" or scheme == "https"


def is_databricks_uri(uri):
    """
    Databricks URIs look like 'databricks' (default profile) or 'databricks://profile'
    or 'databricks://secret_scope:secret_key_prefix'.
    """
    scheme = urllib.parse.urlparse(uri).scheme
    return scheme == "databricks" or uri == "databricks"


def is_databricks_unity_catalog_uri(uri):
    scheme = urllib.parse.urlparse(uri).scheme
    return scheme == _DATABRICKS_UNITY_CATALOG_SCHEME or uri == _DATABRICKS_UNITY_CATALOG_SCHEME


def construct_db_uri_from_profile(profile):
    if profile:
        return "databricks://" + profile


# Both scope and key_prefix should not contain special chars for URIs, like '/'
# and ':'.
def validate_db_scope_prefix_info(scope, prefix):
    for c in ["/", ":", " "]:
        if c in scope:
            raise MlflowException(
                "Unsupported Databricks profile name: %s." % scope
                + " Profile names cannot contain '%s'." % c
            )
        if prefix and c in prefix:
            raise MlflowException(
                "Unsupported Databricks profile key prefix: %s." % prefix
                + " Key prefixes cannot contain '%s'." % c
            )
    if prefix is not None and prefix.strip() == "":
        raise MlflowException(
            "Unsupported Databricks profile key prefix: '%s'." % prefix
            + " Key prefixes cannot be empty."
        )


def get_db_info_from_uri(uri):
    """
    Get the Databricks profile specified by the tracking URI (if any), otherwise
    returns None.
    """
    parsed_uri = urllib.parse.urlparse(uri)
    if parsed_uri.scheme == "databricks" or parsed_uri.scheme == _DATABRICKS_UNITY_CATALOG_SCHEME:
        # netloc should not be an empty string unless URI is formatted incorrectly.
        if parsed_uri.netloc == "":
            raise MlflowException(
                "URI is formatted incorrectly: no netloc in URI '%s'." % uri
                + " This may be the case if there is only one slash in the URI."
            )
        profile_tokens = parsed_uri.netloc.split(":")
        parsed_scope = profile_tokens[0]
        if len(profile_tokens) == 1:
            parsed_key_prefix = None
        elif len(profile_tokens) == 2:
            parsed_key_prefix = profile_tokens[1]
        else:
            # parse the content before the first colon as the profile.
            parsed_key_prefix = ":".join(profile_tokens[1:])
        validate_db_scope_prefix_info(parsed_scope, parsed_key_prefix)
        return parsed_scope, parsed_key_prefix
    return None, None


def get_databricks_profile_uri_from_artifact_uri(uri, result_scheme="databricks"):
    """
    Retrieves the netloc portion of the URI as a ``databricks://`` or `databricks-uc://` URI,
    if it is a proper Databricks profile specification, e.g.
    ``profile@databricks`` or ``secret_scope:key_prefix@databricks``.
    """
    parsed = urllib.parse.urlparse(uri)
    if not parsed.netloc or parsed.hostname != result_scheme:
        return None
    if not parsed.username:  # no profile or scope:key
        return result_scheme  # the default tracking/registry URI
    validate_db_scope_prefix_info(parsed.username, parsed.password)
    key_prefix = ":" + parsed.password if parsed.password else ""
    return f"{result_scheme}://" + parsed.username + key_prefix


def remove_databricks_profile_info_from_artifact_uri(artifact_uri):
    """
    Only removes the netloc portion of the URI if it is a Databricks
    profile specification, e.g.
    ``profile@databricks`` or ``secret_scope:key_prefix@databricks``.
    """
    parsed = urllib.parse.urlparse(artifact_uri)
    if not parsed.netloc or parsed.hostname != "databricks":
        return artifact_uri
    return urllib.parse.urlunparse(parsed._replace(netloc=""))


def add_databricks_profile_info_to_artifact_uri(artifact_uri, databricks_profile_uri):
    """
    Throws an exception if ``databricks_profile_uri`` is not valid.
    """
    if not databricks_profile_uri or not is_databricks_uri(databricks_profile_uri):
        return artifact_uri
    artifact_uri_parsed = urllib.parse.urlparse(artifact_uri)
    # Do not overwrite the authority section if there is already one
    if artifact_uri_parsed.netloc:
        return artifact_uri

    scheme = artifact_uri_parsed.scheme
    if scheme == "dbfs" or scheme == "runs" or scheme == "models":
        if databricks_profile_uri == "databricks":
            netloc = "databricks"
        else:
            (profile, key_prefix) = get_db_info_from_uri(databricks_profile_uri)
            prefix = ":" + key_prefix if key_prefix else ""
            netloc = profile + prefix + "@databricks"
        new_parsed = artifact_uri_parsed._replace(netloc=netloc)
        return urllib.parse.urlunparse(new_parsed)
    else:
        return artifact_uri


def extract_db_type_from_uri(db_uri):
    """
    Parse the specified DB URI to extract the database type. Confirm the database type is
    supported. If a driver is specified, confirm it passes a plausible regex.
    """
    scheme = urllib.parse.urlparse(db_uri).scheme
    scheme_plus_count = scheme.count("+")

    if scheme_plus_count == 0:
        db_type = scheme
    elif scheme_plus_count == 1:
        db_type, _ = scheme.split("+")
    else:
        error_msg = f"Invalid database URI: '{db_uri}'. {_INVALID_DB_URI_MSG}"
        raise MlflowException(error_msg, INVALID_PARAMETER_VALUE)

    _validate_db_type_string(db_type)

    return db_type


def get_uri_scheme(uri_or_path):
    scheme = urllib.parse.urlparse(uri_or_path).scheme
    if any(scheme.lower().startswith(db) for db in DATABASE_ENGINES):
        return extract_db_type_from_uri(uri_or_path)
    return scheme


def extract_and_normalize_path(uri):
    parsed_uri_path = urllib.parse.urlparse(uri).path
    normalized_path = posixpath.normpath(parsed_uri_path)
    return normalized_path.lstrip("/")


def append_to_uri_path(uri, *paths):
    """
    Appends the specified POSIX `paths` to the path component of the specified `uri`.

    :param uri: The input URI, represented as a string.
    :param paths: The POSIX paths to append to the specified `uri`'s path component.
    :return: A new URI with a path component consisting of the specified `paths` appended to
             the path component of the specified `uri`.

    >>> uri1 = "s3://root/base/path?param=value"
    >>> uri1 = append_to_uri_path(uri1, "some/subpath", "/anotherpath")
    >>> assert uri1 == "s3://root/base/path/some/subpath/anotherpath?param=value"
    >>> uri2 = "a/posix/path"
    >>> uri2 = append_to_uri_path(uri2, "/some", "subpath")
    >>> assert uri2 == "a/posixpath/some/subpath"
    """
    path = ""
    for subpath in paths:
        path = _join_posixpaths_and_append_absolute_suffixes(path, subpath)

    parsed_uri = urllib.parse.urlparse(uri)
    if len(parsed_uri.scheme) == 0:
        # If the input URI does not define a scheme, we assume that it is a POSIX path
        # and join it with the specified input paths
        return _join_posixpaths_and_append_absolute_suffixes(uri, path)

    prefix = ""
    if not parsed_uri.path.startswith("/"):
        # For certain URI schemes (e.g., "file:"), urllib's unparse routine does
        # not preserve the relative URI path component properly. In certain cases,
        # urlunparse converts relative paths to absolute paths. We introduce this logic
        # to circumvent urlunparse's erroneous conversion
        prefix = parsed_uri.scheme + ":"
        parsed_uri = parsed_uri._replace(scheme="")

    new_uri_path = _join_posixpaths_and_append_absolute_suffixes(parsed_uri.path, path)
    new_parsed_uri = parsed_uri._replace(path=new_uri_path)
    return prefix + urllib.parse.urlunparse(new_parsed_uri)


def append_to_uri_query_params(uri, *query_params: Tuple[str, Any]) -> str:
    """
    Appends the specified query parameters to an existing URI.

    :param uri: The URI to which to append query parameters.
    :param query_params: Query parameters to append. Each parameter should
                         be a 2-element tuple. For example, ``("key", "value")``.
    """
    parsed_uri = urllib.parse.urlparse(uri)
    parsed_query = urllib.parse.parse_qsl(parsed_uri.query)
    new_parsed_query = parsed_query + list(query_params)
    new_query = urllib.parse.urlencode(new_parsed_query)
    new_parsed_uri = parsed_uri._replace(query=new_query)
    return urllib.parse.urlunparse(new_parsed_uri)


def _join_posixpaths_and_append_absolute_suffixes(prefix_path, suffix_path):
    """
    Joins the POSIX path `prefix_path` with the POSIX path `suffix_path`. Unlike posixpath.join(),
    if `suffix_path` is an absolute path, it is appended to prefix_path.

    >>> result1 = _join_posixpaths_and_append_absolute_suffixes("relpath1", "relpath2")
    >>> assert result1 == "relpath1/relpath2"
    >>> result2 = _join_posixpaths_and_append_absolute_suffixes("relpath", "/absolutepath")
    >>> assert result2 == "relpath/absolutepath"
    >>> result3 = _join_posixpaths_and_append_absolute_suffixes("/absolutepath", "relpath")
    >>> assert result3 == "/absolutepath/relpath"
    >>> result4 = _join_posixpaths_and_append_absolute_suffixes("/absolutepath1", "/absolutepath2")
    >>> assert result4 == "/absolutepath1/absolutepath2"
    """
    if len(prefix_path) == 0:
        return suffix_path

    # If the specified prefix path is non-empty, we must relativize the suffix path by removing
    # the leading slash, if present. Otherwise, posixpath.join() would omit the prefix from the
    # joined path
    suffix_path = suffix_path.lstrip(posixpath.sep)
    return posixpath.join(prefix_path, suffix_path)


def is_databricks_acled_artifacts_uri(artifact_uri):
    _ACLED_ARTIFACT_URI = "databricks/mlflow-tracking/"
    artifact_uri_path = extract_and_normalize_path(artifact_uri)
    return artifact_uri_path.startswith(_ACLED_ARTIFACT_URI)


def is_databricks_model_registry_artifacts_uri(artifact_uri):
    _MODEL_REGISTRY_ARTIFACT_URI = "databricks/mlflow-registry/"
    artifact_uri_path = extract_and_normalize_path(artifact_uri)
    return artifact_uri_path.startswith(_MODEL_REGISTRY_ARTIFACT_URI)


def is_valid_dbfs_uri(uri):
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "dbfs":
        return False
    try:
        db_profile_uri = get_databricks_profile_uri_from_artifact_uri(uri)
    except MlflowException:
        db_profile_uri = None
    return not parsed.netloc or db_profile_uri is not None


def dbfs_hdfs_uri_to_fuse_path(dbfs_uri):
    """
    Converts the provided DBFS URI into a DBFS FUSE path
    :param dbfs_uri: A DBFS URI like "dbfs:/my-directory". Can also be a scheme-less URI like
                     "/my-directory" if running in an environment where the default HDFS filesystem
                     is "dbfs:/" (e.g. Databricks)
    :return A DBFS FUSE-style path, e.g. "/dbfs/my-directory"
    """
    if not is_valid_dbfs_uri(dbfs_uri) and dbfs_uri == posixpath.abspath(dbfs_uri):
        # Convert posixpaths (e.g. "/tmp/mlflow") to DBFS URIs by adding "dbfs:/" as a prefix
        dbfs_uri = "dbfs:" + dbfs_uri
    if not dbfs_uri.startswith(_DBFS_HDFS_URI_PREFIX):
        raise MlflowException(
            f"Path '{dbfs_uri}' did not start with expected DBFS URI "
            f"prefix '{_DBFS_HDFS_URI_PREFIX}'",
        )

    return _DBFS_FUSE_PREFIX + dbfs_uri[len(_DBFS_HDFS_URI_PREFIX) :]


def resolve_uri_if_local(local_uri):
    """
    if `local_uri` is passed in as a relative local path, this function
    resolves it to absolute path relative to current working directory.

    :param local_uri: Relative or absolute path or local file uri

    :return: a fully-formed absolute uri path or an absolute filesystem path
    """
    from mlflow.utils.file_utils import local_file_uri_to_path

    if local_uri is not None and is_local_uri(local_uri):
        scheme = get_uri_scheme(local_uri)
        cwd = pathlib.Path.cwd()
        local_path = local_file_uri_to_path(local_uri)
        if not pathlib.Path(local_path).is_absolute():
            if scheme == "":
                if is_windows():
                    return urllib.parse.urlunsplit(
                        (
                            "file",
                            None,
                            cwd.joinpath(local_path).as_posix(),
                            None,
                            None,
                        )
                    )
                return cwd.joinpath(local_path).as_posix()
            local_uri_split = urllib.parse.urlsplit(local_uri)
            resolved_absolute_uri = urllib.parse.urlunsplit(
                (
                    local_uri_split.scheme,
                    None,
                    cwd.joinpath(local_path).as_posix(),
                    local_uri_split.query,
                    local_uri_split.fragment,
                )
            )
            return resolved_absolute_uri
    return local_uri


def generate_tmp_dfs_path(dfs_tmp):
    return posixpath.join(dfs_tmp, str(uuid.uuid4()))
