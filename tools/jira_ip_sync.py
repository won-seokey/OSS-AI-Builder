import requests
import sys
import os
import time
import io
import urllib3

# Windows 한글 인코딩 강제 설정
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= [ 설정값 입력 ] =================
ORG_NAME = "example-org"
ATLASSIAN_IP_URL = "https://ip-ranges.atlassian.com/"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
TARGET_PRODUCTS = {"jira", "github-for-jira"}
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2
# ==================================================


def resolve_github_token():
    """Jenkins Credentials가 주입한 환경변수 GITHUB_TOKEN을 읽는다."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    print("ERROR: GITHUB_TOKEN 환경변수를 찾지 못했습니다.")
    print("   Jenkins Credentials(Secret text)로 GITHUB_TOKEN을 주입하도록 설정하세요.")
    print("   예시: withCredentials([string(credentialsId: 'GITHUB_TOKEN', variable: 'GITHUB_TOKEN')])")
    sys.exit(1)


GITHUB_TOKEN = resolve_github_token()
print("INFO: 토큰 소스: Jenkins Credentials 환경변수(GITHUB_TOKEN)")

headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Content-Type": "application/json"
}


def request_json(method, url, **kwargs):
    """HTTP JSON 요청을 재시도/상태코드 검사와 함께 수행한다."""
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(
                method,
                url,
                timeout=REQUEST_TIMEOUT,
                verify=False,
                **kwargs,
            )

            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(
                    f"HTTP {response.status_code}: {response.text[:300]}",
                    response=response,
                )

            response.raise_for_status()

            try:
                return response, response.json()
            except ValueError as error:
                raise ValueError(
                    f"JSON 파싱 실패: {error}; 응답 본문 일부: {response.text[:300]}"
                ) from error

        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt == MAX_RETRIES:
                break
            print(
                f"WARN: API 요청 실패, {RETRY_DELAY_SECONDS}초 후 재시도합니다. "
                f"({attempt}/{MAX_RETRIES}) - {error}"
            )
            time.sleep(RETRY_DELAY_SECONDS)

    print(f"ERROR: API 요청 실패: {url} - {last_error}")
    sys.exit(1)


def get_atlassian_outbound_ips():
    """Atlassian 공식 사이트에서 Jira 관련 egress IPv4 CIDR 목록 추출"""
    try:
        res, response = request_json("GET", ATLASSIAN_IP_URL)

        if "json" not in res.headers.get("Content-Type", ""):
            print("ERROR: [보안 차단 의심] Atlassian이 JSON이 아닌 다른 응답을 보냈습니다.")
            print(f"상태 코드: {res.status_code}")
            print(f"실제 서버 응답 내용 요약:\n{res.text[:500]}")
            sys.exit(1)

        ips = set()
        for item in response.get("items", []):
            cidr = item.get("cidr", "")
            directions = item.get("direction", [])
            products = set(item.get("product", []))
            if (
                ":" not in cidr
                and "egress" in directions
                and products.intersection(TARGET_PRODUCTS)
            ):
                ips.add(cidr)
        return list(ips)
    except Exception as e:
        print(f"ERROR: 에러 발생: {e}")
        sys.exit(1)


def get_org_id():
    """GitHub GraphQL API로 조직 Node ID 조회"""
    query = """
    query($login: String!) {
        organization(login: $login) {
            id
        }
    }
    """
    _, data = request_json(
        "POST",
        GITHUB_GRAPHQL_URL,
        headers=headers,
        json={"query": query, "variables": {"login": ORG_NAME}},
    )
    if "errors" in data:
        print(f"ERROR: 조직 ID 조회 실패: {data['errors']}")
        sys.exit(1)
    return data["data"]["organization"]["id"]


def get_existing_ips(org_id):
    """GitHub GraphQL API로 기존 IP allow list 항목 전체 조회 (페이지네이션 포함)
    
    반환값: (atlassian_managed_ips, managed_ips_map, all_ips)
        - atlassian_managed_ips: "Atlassian-Jira-Outbound" 이름의 IP 집합
        - managed_ips_map: IP -> {id, name} 매핑
        - all_ips: 모든 등록된 IP 집합 (수동 항목 포함)
    """
    query = """
    query($id: ID!, $cursor: String) {
        node(id: $id) {
            ... on Organization {
                ipAllowListEntries(first: 100, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes { id allowListValue name }
                }
            }
        }
    }
    """
    atlassian_managed = set()
    managed_map = {}
    all_ips = set()
    cursor = None
    
    while True:
        _, data = request_json(
            "POST",
            GITHUB_GRAPHQL_URL,
            headers=headers,
            json={"query": query, "variables": {"id": org_id, "cursor": cursor}},
        )
        node_data = data.get("data", {}).get("node")
        if not node_data or "ipAllowListEntries" not in node_data:
            print(f"ERROR: 기존 IP 목록 응답 형식이 올바르지 않습니다: {data}")
            sys.exit(1)

        entries = node_data["ipAllowListEntries"]
        for node in entries["nodes"]:
            ip = node["allowListValue"]
            entry_id = node["id"]
            entry_name = node.get("name", "")
            
            all_ips.add(ip)
            
            if entry_name == "Atlassian-Jira-Outbound":
                atlassian_managed.add(ip)
                managed_map[ip] = {"id": entry_id, "name": entry_name}
        
        if not entries["pageInfo"]["hasNextPage"]:
            break
        cursor = entries["pageInfo"]["endCursor"]
    
    return atlassian_managed, managed_map, all_ips


def add_ip_to_github(org_id, cidr_ip):
    """GitHub GraphQL API로 IP Allow List에 항목 추가"""
    mutation = """
    mutation($input: CreateIpAllowListEntryInput!) {
        createIpAllowListEntry(input: $input) {
            ipAllowListEntry {
                id
                allowListValue
            }
        }
    }
    """
    variables = {
        "input": {
            "ownerId": org_id,
            "allowListValue": cidr_ip,
            "name": "Atlassian-Jira-Outbound",
            "isActive": True
        }
    }
    _, data = request_json(
        "POST",
        GITHUB_GRAPHQL_URL,
        headers=headers,
        json={"query": mutation, "variables": variables},
    )
    if "errors" in data:
        print(f"ERROR: 등록 실패: {cidr_ip} - {data['errors']}")
    else:
        print(f"INFO: 추가 성공: {cidr_ip}")


def remove_ip_from_github(entry_id, cidr_ip):
    """GitHub GraphQL API로 IP Allow List에서 항목 제거
    
    주의: "Atlassian-Jira-Outbound" 이름의 항목만 제거하도록 호출해야 함
    """
    mutation = """
    mutation($input: DeleteIpAllowListEntryInput!) {
        deleteIpAllowListEntry(input: $input) {
            ipAllowListEntry {
                id
                allowListValue
            }
        }
    }
    """
    variables = {
        "input": {
            "ipAllowListEntryId": entry_id
        }
    }
    _, data = request_json(
        "POST",
        GITHUB_GRAPHQL_URL,
        headers=headers,
        json={"query": mutation, "variables": variables},
    )
    if "errors" in data:
        print(f"ERROR: 제거 실패: {cidr_ip} - {data['errors']}")
    else:
        print(f"INFO: 제거 성공: {cidr_ip}")


if __name__ == "__main__":
    print("INFO: Atlassian 최신 아웃바운드 IP 목록을 가져오는 중...")
    atlassian_ips = get_atlassian_outbound_ips()
    print(f"INFO: 총 {len(atlassian_ips)}개의 Jira IP 대역이 확인되었습니다.")

    print("INFO: GitHub 조직 정보 조회 중...")
    org_id = get_org_id()

    print("INFO: 기존 IP Allow List 조회 중...")
    atlassian_managed_ips, managed_ips_map, all_ips = get_existing_ips(org_id)
    manual_ips = all_ips - atlassian_managed_ips
    print(f"INFO: 전체 {len(all_ips)}개 (Atlassian 관리: {len(atlassian_managed_ips)}개, 수동: {len(manual_ips)}개)")

    new_ips = [ip for ip in atlassian_ips if ip not in atlassian_managed_ips]
    print(f"INFO: 신규 추가 대상: {len(new_ips)}개")

    for ip in new_ips:
        add_ip_to_github(org_id, ip)

    removed_ips = [ip for ip in atlassian_managed_ips if ip not in atlassian_ips]
    print(f"INFO: 제거 대상 (Atlassian 관리만): {len(removed_ips)}개")

    for ip in removed_ips:
        entry_info = managed_ips_map.get(ip)
        if entry_info:
            remove_ip_from_github(entry_info["id"], ip)
        else:
            print(f"WARN: 제거할 항목의 정보를 찾지 못함: {ip}")

    print("INFO: 모든 Jira IP 대역 동기화 작업이 완료되었습니다!")
