"""Main app entrypoint. Starts Uvicorn-based REST API service."""

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

import certifi
import uvicorn
from cryptography import x509

import ols.app.models.config as config_model
from ols import constants
from ols.src.auth.auth import use_k8s_auth
from ols.utils import tls
from ols.utils.logging import configure_logging


def configure_hugging_face_envs(ols_config: config_model.OLSConfig) -> None:
    """Configure HuggingFace library environment variables."""
    if (
        ols_config
        and hasattr(ols_config, "reference_content")
        and hasattr(ols_config.reference_content, "embeddings_model_path")
        and ols_config.reference_content.embeddings_model_path
    ):
        os.environ["TRANSFORMERS_CACHE"] = str(
            ols_config.reference_content.embeddings_model_path
        )
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def configure_gradio_ui_envs() -> None:
    """Configure GradioUI framework environment variables."""
    # disable Gradio analytics, which calls home to https://api.gradio.app
    os.environ["GRADIO_ANALYTICS_ENABLED"] = "false"

    # Setup config directory for Matplotlib. It will be used to store info
    # about fonts (usually one JSON file) and it really is just temporary
    # storage that can be deleted at any time and recreated later.
    # Fixes: https://issues.redhat.com/browse/OLS-301
    tempdir = os.path.join(tempfile.gettempdir(), "matplotlib")
    os.environ["MPLCONFIGDIR"] = tempdir


def load_index():
    """Load the index."""
    # accessing the config's rag_index property will trigger the loading
    # of the index
    config.rag_index


def add_ca_to_certificates_store(
    logger: logging.Logger, cert_path: Path, cert_location: str
) -> None:
    """Add a certificate to the certifi store."""
    logger.debug("Certifications store location: %s", cert_location)
    logger.info("Adding certificate '%s' to certificates store", cert_path)

    # load certificate file that needs to be added into store
    with open(cert_path, "rb") as certificate_file:
        new_certificate_data = certificate_file.read()
    new_cert = x509.load_pem_x509_certificate(new_certificate_data)

    # load existing certificates
    with open(cert_location, "rb") as certifi_store:
        certifi_certs_data = certifi_store.read()
    certifi_certs = x509.load_pem_x509_certificates(certifi_certs_data)

    # append the certificate to the certificates store
    if new_cert in certifi_certs:
        logger.warning("Certificate '%s' is already in certificates store", cert_path)
    else:
        with open(cert_location, "ab") as certifi_store:
            certifi_store.write(new_certificate_data)
            logger.debug(
                "Written certificate with length %d bytes", len(new_certificate_data)
            )


def generate_certificates_file(
    logger: logging.Logger, ols_config: config_model.OLSConfig
) -> None:
    """Generate certificates by merging certificates from certify with defined certificates."""
    certificate_directory = ols_config.certificate_directory

    logger.info("Generating certificates file into directory %s", certificate_directory)

    # file where all certificates will be stored
    destination_file = os.path.join(
        certificate_directory, constants.CERTIFICATE_STORAGE_FILENAME
    )

    certifi_cert_location = certifi.where()
    logger.debug(
        "Copying certifi certificates file from %s into %s",
        certifi_cert_location,
        destination_file,
    )

    shutil.copyfile(certifi_cert_location, destination_file)

    for certificate_path in ols_config.extra_ca:
        add_ca_to_certificates_store(logger, certificate_path, destination_file)


def get_ssl_version(sec_profile):
    """Get SSL version to be used. It can be configured in tls_security_profile section."""
    # if security profile is not set, use default SSL version
    # as specified in SSL library
    if sec_profile is None or sec_profile.profile_type is None:
        logger.info("Using default SSL version: %s", constants.DEFAULT_SSL_VERSION)
        return constants.DEFAULT_SSL_VERSION

    # security profile is set -> we need to retrieve SSL version and list of allowed ciphers
    min_tls_version = tls.min_tls_version(
        sec_profile.min_tls_version, sec_profile.profile_type
    )
    logger.info("min TLS version: %s", min_tls_version)

    ssl_version = tls.ssl_tls_version(min_tls_version)
    logger.info("Using SSL version: %s", ssl_version)
    return ssl_version


def get_ciphers(sec_profile):
    """Get allowed ciphers to be used. It can be configured in tls_security_profile section."""
    # if security profile is not set, use default ciphers
    # as specified in SSL library
    if sec_profile is None or sec_profile.profile_type is None:
        logger.info("Allowing default ciphers: %s", constants.DEFAULT_SSL_CIPHERS)
        return constants.DEFAULT_SSL_CIPHERS

    # security profile is set -> we need to retrieve ciphers to be allowed
    ciphers = tls.ciphers_as_string(sec_profile.ciphers, sec_profile.profile_type)
    logger.info("Allowing following ciphers: %s", ciphers)
    return ciphers


def start_uvicorn():
    """Start Uvicorn-based REST API service."""
    # use workers=1 so config loaded can be accessed from other modules
    host = (
        "localhost"
        if config.dev_config.run_on_localhost
        else "0.0.0.0"  # noqa: S104 # nosec: B104
    )
    port = 8080 if config.dev_config.disable_tls else 8443
    log_level = config.ols_config.logging_config.uvicorn_log_level

    # The tls fields can be None, which means we will pass those values as None to uvicorn.run
    ssl_keyfile = config.ols_config.tls_config.tls_key_path
    ssl_certfile = config.ols_config.tls_config.tls_certificate_path
    ssl_keyfile_password = config.ols_config.tls_config.tls_key_password

    # setup SSL version and allowed SSL ciphers based on service configuration
    # when TLS security profile is not specified, default values will be used
    # that default values are based on default SSL package settings
    sec_profile = config.ols_config.tls_security_profile
    ssl_version = get_ssl_version(sec_profile)
    ssl_ciphers = get_ciphers(sec_profile)

    uvicorn.run(
        "ols.app.main:app",
        host=host,
        port=port,
        workers=config.ols_config.max_workers,
        log_level=log_level,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
        ssl_keyfile_password=ssl_keyfile_password,
        ssl_version=ssl_version,
        ssl_ciphers=ssl_ciphers,
        access_log=log_level < logging.INFO,
    )


if __name__ == "__main__":
    # First of all, configure environment variables for Gradio before
    # import config and initializing config module.
    configure_gradio_ui_envs()

    # NOTE: We import config here to avoid triggering import of anything
    # else via our code before other envs are set (mainly the gradio).
    from ols import config

    cfg_file = os.environ.get("RCS_CONFIG_FILE", "rcsconfig.yaml")
    config.reload_from_yaml_file(cfg_file)

    configure_logging(config.ols_config.logging_config)
    logger = logging.getLogger("ols")
    logger.info("Config loaded from %s", Path(cfg_file).resolve())

    configure_hugging_face_envs(config.ols_config)

    # generate certificates file from all certificates from certifi package
    # merged with explicitly specified certificates
    generate_certificates_file(logger, config.ols_config)

    if use_k8s_auth(config.ols_config):
        logger.info("Initializing k8s auth")
        # from ols.src.auth.k8s import K8sClientSingleton

        # Initialize the K8sClientSingleton with cluster id during module load.
        # We want the application to fail early if the cluster ID is not available.
        # comment in upstream for now!
        # cluster_id = K8sClientSingleton.get_cluster_id()
        # logger.info("running on cluster with ID '%s'", cluster_id)

    # init loading of query redactor
    config.query_redactor

    # create and start the rag_index_thread - allows loading index in
    # parallel with starting the Uvicorn server
    rag_index_thread = threading.Thread(target=load_index)
    rag_index_thread.start()

    # start the Uvicorn server
    start_uvicorn()
