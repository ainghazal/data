import hashlib
import abc
import logging

from dataclasses import dataclass, field
from urllib.parse import urlparse, urlsplit
from datetime import datetime, timedelta
from typing import Generator, Optional, List, Dict

from oonidata.dataformat import (
    BaseMeasurement,
    DNSAnswer,
    DNSQuery,
    HTTPTransaction,
    Failure,
    NetworkEvent,
    TCPConnect,
    TLSHandshake,
)

from oonidata.datautils import (
    get_first_http_header,
    get_html_meta_title,
    get_html_title,
    is_ipv4_bogon,
    is_ipv6_bogon,
    get_certificate_meta,
)
from oonidata.fingerprints.matcher import FingerprintDB
from oonidata.netinfo import NetinfoDB


log = logging.getLogger("oonidata.processing")


def normalize_failure(failure: Failure):
    # TODO: implement a mapping between known unknowns to cleanup the data a bit
    return failure


@dataclass
class Observation(abc.ABC):
    __table_name__ = "obs"

    measurement_uid: str
    observation_id: str

    session_id: str

    timestamp: datetime

    target: str

    probe_asn: int
    probe_cc: str

    probe_as_org_name: str
    probe_as_cc: str

    software_name: str
    software_version: str
    network_type: str
    platform: str
    origin: str

    resolver_asn: int
    resolver_ip: str
    resolver_cc: str
    resolver_as_org_name: str
    resolver_as_cc: str


def make_base_observation_meta(msmt: BaseMeasurement, netinfodb: NetinfoDB) -> dict:
    assert msmt.measurement_uid is not None
    probe_asn = int(msmt.probe_asn.lstrip("AS"))
    measurement_start_time = datetime.strptime(
        msmt.measurement_start_time, "%Y-%m-%d %H:%M:%S"
    )
    probe_as_info = netinfodb.lookup_asn(measurement_start_time, probe_asn)

    resolver_as_info = None
    resolver_ip = msmt.resolver_ip
    if not resolver_ip and msmt.test_keys and msmt.test_keys.client_resolver:
        resolver_ip = msmt.test_keys.client_resolver
    if resolver_ip:
        resolver_as_info = netinfodb.lookup_ip(measurement_start_time, resolver_ip)

    return dict(
        measurement_uid=msmt.measurement_uid,
        probe_asn=probe_asn,
        probe_cc=msmt.probe_cc,
        probe_as_org_name=probe_as_info.as_org_name if probe_as_info else "",
        probe_as_cc=probe_as_info.as_cc if probe_as_info else "",
        session_id=msmt.report_id,
        software_name=msmt.software_name,
        software_version=msmt.software_version,
        network_type=msmt.annotations.network_type,
        platform=msmt.annotations.platform,
        origin=msmt.annotations.origin,
        target="",
        resolver_ip=resolver_ip if resolver_ip else "",
        resolver_cc=resolver_as_info.cc if resolver_as_info else "",
        resolver_asn=resolver_as_info.as_info.asn if resolver_as_info else 0,
        resolver_as_org_name=resolver_as_info.as_info.as_org_name
        if resolver_as_info
        else "",
        resolver_as_cc=resolver_as_info.as_info.as_cc if resolver_as_info else "",
    )


def make_timestamp(msmt: BaseMeasurement, t: Optional[float] = None):
    timestamp = datetime.strptime(msmt.measurement_start_time, "%Y-%m-%d %H:%M:%S")
    if t:
        timestamp += timedelta(seconds=t)
    return timestamp


@dataclass
class HTTPObservation(Observation):
    __table_name__ = "obs_http"

    domain_name: str
    request_url: str
    request_is_encrypted: bool

    failure: Failure

    request_body_length: int
    # request_headers_list: Optional[List[Tuple[str, bytes]]]
    request_method: str

    response_fingerprints: List[str]

    response_body_length: Optional[int] = None
    response_body_is_truncated: Optional[bool] = None
    response_body_sha1: Optional[str] = None
    response_body_title: Optional[str] = None
    response_body_meta_title: Optional[str] = None

    response_status_code: Optional[int] = None
    # response_headers_list: Optional[List[Tuple[str, bytes]]]
    response_header_location: Optional[bytes] = None
    response_header_server: Optional[bytes] = None
    request_redirect_from: Optional[str] = None
    request_body_is_truncated: Optional[bool] = None

    fingerprint_country_consistent: Optional[bool] = None
    response_matches_blockpage: bool = False
    response_matches_false_positive: bool = False
    x_transport: Optional[str] = "tcp"

    @staticmethod
    def from_measurement(
        msmt: BaseMeasurement,
        netinfodb: NetinfoDB,
        idx: int,
        requests_list: Optional[List[HTTPTransaction]],
        http_transaction: HTTPTransaction,
        fingerprintdb: FingerprintDB,
    ) -> Optional["HTTPObservation"]:
        if not http_transaction.request:
            # This is a very malformed request, we don't consider it a valid
            # observation as we don't know what it's referring to.
            # XXX maybe log this somewhere
            return None

        parsed_url = urlparse(http_transaction.request.url)
        hrro = HTTPObservation(
            observation_id=f"{msmt.measurement_uid}{idx}",
            request_url=http_transaction.request.url,
            domain_name=parsed_url.hostname or "",
            request_is_encrypted=parsed_url.scheme == "https",
            request_body_is_truncated=http_transaction.request.body_is_truncated,
            # hrro.request_headers_list = http_transaction.request.headers_list_bytes
            request_method=http_transaction.request.method or "",
            request_body_length=len(http_transaction.request.body_bytes)
            if http_transaction.request.body_bytes
            else 0,
            response_fingerprints=[],
            x_transport=http_transaction.request.x_transport,
            failure=normalize_failure(http_transaction.failure),
            timestamp=make_timestamp(msmt, http_transaction.t),
            **make_base_observation_meta(msmt, netinfodb),
        )

        if not http_transaction.response:
            return hrro

        hrro.response_body_is_truncated = http_transaction.response.body_is_truncated

        fp_matches = fingerprintdb.match_http(http_transaction.response)
        for fp in fp_matches:
            if fp.scope == "fp":
                hrro.response_matches_false_positive = True
            else:
                hrro.response_matches_blockpage = True
            if fp.expected_countries and msmt.probe_cc in fp.expected_countries:
                hrro.fingerprint_country_consistent = True
            hrro.response_fingerprints.append(fp.name)

        if http_transaction.response.body_bytes:
            hrro.response_body_length = len(http_transaction.response.body_bytes)
            hrro.response_body_sha1 = hashlib.sha1(
                http_transaction.response.body_bytes
            ).hexdigest()
            hrro.response_body_title = get_html_title(
                http_transaction.response.body_bytes
            )
            hrro.response_body_meta_title = get_html_meta_title(
                http_transaction.response.body_bytes
            )

        hrro.response_status_code = http_transaction.response.code
        # hrro.response_headers_list = http_transaction.response.headers_list_bytes

        hrro.response_header_location = get_first_http_header(
            "location", http_transaction.response.headers_list_bytes or []
        )
        hrro.response_header_server = get_first_http_header(
            "server", http_transaction.response.headers_list_bytes or []
        )

        try:
            # We add type: ignore in here, because requests_lists is an optional
            # field and the fact it might not be defined is handled by the
            # except block below, yet pylint is not able to figure that out.
            # TODO: maybe refactor this handle it better by checking if these are defined
            prev_request = requests_list[idx + 1]  # type: ignore
            prev_location = get_first_http_header(
                "location", prev_request.response.headers_list_bytes or []  # type: ignore
            ).decode("utf-8")
            if prev_location == hrro.request_url:
                hrro.request_redirect_from = prev_request.request.url  # type: ignore
        except (IndexError, UnicodeDecodeError, AttributeError):
            pass
        return hrro


def make_http_observations(
    msmt: BaseMeasurement,
    requests_list: Optional[List[HTTPTransaction]],
    fingerprintdb: FingerprintDB,
    netinfodb: NetinfoDB,
    target: str = "",
) -> Generator[HTTPObservation, None, None]:
    if not requests_list:
        return

    for idx, http_transaction in enumerate(requests_list):
        httpo = HTTPObservation.from_measurement(
            msmt, netinfodb, idx, requests_list, http_transaction, fingerprintdb
        )
        if httpo:
            httpo.target = target
            yield httpo


@dataclass
class DNSObservation(Observation):
    __table_name__ = "obs_dns"

    domain_name: str

    query_type: str
    failure: Failure
    engine: Optional[str]
    engine_resolver_address: Optional[str]

    answer_type: Optional[str] = None
    answer: Optional[str] = None
    answer_asn: Optional[int] = None
    answer_as_org_name: Optional[str] = None
    answer_as_cc: Optional[str] = None
    answer_cc: Optional[str] = None
    answer_is_bogon: Optional[bool] = None

    fingerprint_id: Optional[str] = None
    fingerprint_country_consistent: Optional[bool] = None

    is_tls_consistent: Optional[bool] = None

    @staticmethod
    def from_measurement(
        msmt: BaseMeasurement,
        query: DNSQuery,
        answer: Optional[DNSAnswer],
        idx: int,
        fingerprintdb: FingerprintDB,
        netinfodb: NetinfoDB,
    ) -> "DNSObservation":
        dnso = DNSObservation(
            observation_id=f"{msmt.measurement_uid}{idx}",
            engine=query.engine,
            engine_resolver_address=query.resolver_address,
            query_type=query.query_type,
            domain_name=query.hostname,
            failure=normalize_failure(query.failure),
            timestamp=make_timestamp(msmt, query.t),
            **make_base_observation_meta(msmt, netinfodb),
        )

        if not answer:
            return dnso

        dnso.answer_type = answer.answer_type
        if answer.ipv4:
            dnso.answer = answer.ipv4
            dnso.answer_is_bogon = is_ipv4_bogon(answer.ipv4)
        elif answer.ipv6:
            dnso.answer = answer.ipv6
            dnso.answer_is_bogon = is_ipv6_bogon(answer.ipv6)
        elif answer.hostname:
            dnso.answer = answer.hostname

        if answer.ipv4 or answer.ipv6:
            # This is guaranteed to be the correct type since we set it's value
            # based on answer.ipv4 or answer.ipv6 being set in the previous
            # blocks
            answer_meta = netinfodb.lookup_ip(dnso.timestamp, dnso.answer)  # type: ignore
            if answer_meta:
                dnso.answer_asn = answer_meta.as_info.asn
                dnso.answer_as_cc = answer_meta.as_info.as_cc
                dnso.answer_as_org_name = answer_meta.as_info.as_org_name
                dnso.answer_cc = answer_meta.cc

        matched_fingerprint = fingerprintdb.match_dns(dnso.answer)
        if matched_fingerprint:
            dnso.fingerprint_id = matched_fingerprint.name
            if matched_fingerprint.expected_countries:
                dnso.fingerprint_country_consistent = (
                    msmt.probe_cc in matched_fingerprint.expected_countries
                )
        return dnso


def make_dns_observations(
    msmt: BaseMeasurement,
    queries: Optional[List[DNSQuery]],
    fingerprintdb: FingerprintDB,
    netinfodb: NetinfoDB,
    target: str = "",
) -> Generator[DNSObservation, None, None]:
    if not queries:
        return

    idx = 0
    for query in queries:
        answer_list = query.answers
        if not answer_list:
            answer_list = [None]
        for answer in answer_list:
            dnso = DNSObservation.from_measurement(
                msmt, query, answer, idx, fingerprintdb, netinfodb
            )
            dnso.target = target
            yield dnso
            idx += 1


@dataclass
class TCPObservation(Observation):
    __table_name__ = "obs_tcp"

    domain_name: str

    ip: str
    port: int

    failure: Failure

    ip_asn: Optional[int] = None
    ip_as_org_name: Optional[str] = None
    ip_as_cc: Optional[str] = None
    ip_cc: Optional[str] = None

    @staticmethod
    def from_measurement(
        msmt: BaseMeasurement,
        res: TCPConnect,
        idx: int,
        ip_to_domain: Dict[str, str],
        netinfodb: NetinfoDB,
    ) -> "TCPObservation":
        tcpo = TCPObservation(
            observation_id=f"{msmt.measurement_uid}{idx}",
            timestamp=make_timestamp(msmt, res.t),
            ip=res.ip,
            port=res.port,
            failure=normalize_failure(res.status.failure),
            domain_name=ip_to_domain.get(res.ip, ""),
            **make_base_observation_meta(msmt, netinfodb),
        )

        ip_info = netinfodb.lookup_ip(tcpo.timestamp, res.ip)
        if ip_info:
            tcpo.ip_asn = ip_info.as_info.asn
            tcpo.ip_as_org_name = ip_info.as_info.as_org_name
            tcpo.ip_as_cc = ip_info.as_info.as_cc

            tcpo.ip_cc = ip_info.cc

        return tcpo


def make_tcp_observations(
    msmt: BaseMeasurement,
    tcp_connect: Optional[List[TCPConnect]],
    netinfodb: NetinfoDB,
    ip_to_domain: Dict[str, str] = {},
    target: str = "",
) -> Generator[TCPObservation, None, None]:
    if not tcp_connect:
        return

    for idx, res in enumerate(tcp_connect):
        tcpo = TCPObservation.from_measurement(msmt, res, idx, ip_to_domain, netinfodb)
        tcpo.target = target
        yield tcpo


def network_events_until_connect(
    network_events: List[NetworkEvent],
) -> List[NetworkEvent]:
    ne_list = []
    for ne in network_events:
        if ne.operation == "connect":
            break
        ne_list.append(ne)
    return ne_list


def find_tls_handshake_network_events(
    tls_handshake: TLSHandshake, network_events: Optional[List[NetworkEvent]]
) -> Optional[List[NetworkEvent]]:
    if not network_events:
        return None

    current_event_window = []
    for idx, ne in enumerate(network_events):
        if ne.operation == "connect":
            current_event_window = []
        current_event_window.append(ne)
        # We identify the network_event for the given TLS handshake based on the
        # fact that the timestamp on tls_handshake_done event is the same as the
        # tls_handshake time
        if ne.operation == "tls_handshake_done" and ne.t == tls_handshake.t:
            current_event_window += network_events_until_connect(network_events[idx:])
            return current_event_window
    return None


@dataclass
class TLSObservation(Observation):
    __table_name__ = "obs_tls"

    domain_name: str

    failure: Failure

    server_name: str
    tls_version: str
    cipher_suite: str

    ip: Optional[str] = None
    port: Optional[int] = None

    ip_asn: Optional[int] = None
    ip_as_org_name: Optional[str] = None
    ip_as_cc: Optional[str] = None
    ip_cc: Optional[str] = None

    is_certificate_valid: Optional[bool] = None

    end_entity_certificate_fingerprint: Optional[str] = None
    end_entity_certificate_subject: Optional[str] = None
    end_entity_certificate_subject_common_name: Optional[str] = None
    end_entity_certificate_issuer: Optional[str] = None
    end_entity_certificate_issuer_common_name: Optional[str] = None
    end_entity_certificate_san_list: List[str] = field(default_factory=list)
    end_entity_certificate_not_valid_after: Optional[datetime] = None
    end_entity_certificate_not_valid_before: Optional[datetime] = None
    certificate_chain_length: Optional[int] = None

    tls_handshake_read_count: Optional[int] = None
    tls_handshake_write_count: Optional[int] = None
    tls_handshake_read_bytes: Optional[float] = None
    tls_handshake_write_bytes: Optional[float] = None
    tls_handshake_last_operation: Optional[str] = None
    tls_handshake_time: Optional[float] = None

    @staticmethod
    def from_measurement(
        msmt: BaseMeasurement,
        tls_h: TLSHandshake,
        network_events: Optional[List[NetworkEvent]],
        idx: int,
        ip_to_domain: Dict[str, str],
        netinfodb: NetinfoDB,
    ) -> "TLSObservation":
        tlso = TLSObservation(
            observation_id=f"{msmt.measurement_uid}{idx}",
            timestamp=make_timestamp(msmt, tls_h.t),
            server_name=tls_h.server_name if tls_h.server_name else "",
            domain_name=tls_h.server_name if tls_h.server_name else "",
            tls_version=tls_h.tls_version if tls_h.tls_version else "",
            cipher_suite=tls_h.cipher_suite if tls_h.cipher_suite else "",
            end_entity_certificate_san_list=[],
            failure=normalize_failure(tls_h.failure),
            **make_base_observation_meta(msmt, netinfodb),
        )

        if tls_h.address:
            p = urlsplit("//" + tls_h.address)
            tlso.ip = p.hostname
            tlso.port = p.port

        if tls_h.no_tls_verify == False:
            if tlso.failure in (
                "ssl_invalid_hostname",
                "ssl_unknown_authority",
                "ssl_invalid_certificate",
            ):
                tlso.is_certificate_valid = False
            elif not tlso.failure:
                tlso.is_certificate_valid = True

        tls_network_events = find_tls_handshake_network_events(tls_h, network_events)
        if tls_network_events:
            if tls_network_events[0].address:
                p = urlsplit("//" + tls_network_events[0].address)
                tlso.ip = p.hostname
                tlso.port = p.port
                tlso.domain_name = ip_to_domain.get(tlso.ip or "", "")

            tlso.tls_handshake_time = tls_network_events[-1].t - tls_network_events[0].t
            tlso.tls_handshake_read_count = 0
            tlso.tls_handshake_write_count = 0
            tlso.tls_handshake_read_bytes = 0
            tlso.tls_handshake_write_bytes = 0
            for ne in tls_network_events:
                if ne.operation == "write":
                    if ne.num_bytes:
                        tlso.tls_handshake_write_count += 1
                        tlso.tls_handshake_write_bytes += ne.num_bytes
                    tlso.tls_handshake_last_operation = (
                        f"write_{tlso.tls_handshake_write_count}"
                    )
                elif ne.operation == "read" and ne.num_bytes:
                    if ne.num_bytes:
                        tlso.tls_handshake_read_count += 1
                        tlso.tls_handshake_read_bytes += ne.num_bytes
                    tlso.tls_handshake_last_operation = (
                        f"read_{tlso.tls_handshake_read_count}"
                    )

        if tls_h.peer_certificates:
            tlso.certificate_chain_length = len(tls_h.peer_certificates)
            try:
                cert_meta = get_certificate_meta(tls_h.peer_certificates[0])
                tlso.end_entity_certificate_fingerprint = cert_meta.fingerprint
                tlso.end_entity_certificate_subject = cert_meta.subject
                tlso.end_entity_certificate_subject_common_name = (
                    cert_meta.subject_common_name
                )
                tlso.end_entity_certificate_issuer = cert_meta.issuer
                tlso.end_entity_certificate_issuer_common_name = (
                    cert_meta.issuer_common_name
                )
                tlso.end_entity_certificate_not_valid_after = cert_meta.not_valid_after
                tlso.end_entity_certificate_not_valid_before = (
                    cert_meta.not_valid_before
                )
                tlso.end_entity_certificate_san_list = cert_meta.san_list
            except Exception as exc:
                log.error(exc)
                log.error(
                    f"Failed to extract certificate meta for {msmt.measurement_uid}"
                )

        return tlso


def make_tls_observations(
    msmt: BaseMeasurement,
    tls_handshakes: Optional[List[TLSHandshake]],
    network_events: Optional[List[NetworkEvent]],
    netinfodb: NetinfoDB,
    ip_to_domain: Dict[str, str] = {},
    target: str = "",
) -> Generator[TLSObservation, None, None]:
    if not tls_handshakes:
        return

    for idx, tls_h in enumerate(tls_handshakes):
        tso = TLSObservation.from_measurement(
            msmt, tls_h, network_events, idx, ip_to_domain, netinfodb
        )
        tso.target = target
        yield tso
