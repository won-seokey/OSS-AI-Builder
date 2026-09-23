import requests
import json
from requests.auth import HTTPBasicAuth
import datetime
import os
import sys
import io

# Windows에서 stdout을 UTF-8로 고정해 Jenkins 콘솔/로그 인코딩 깨짐을 방지
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ================= [설정 정보 입력] =================
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()                        # Jenkins credential 주입값
CONF_TOKEN = os.getenv("CONF_TOKEN", "").strip()                    # Jenkins credential 주입값
CONF_USER = os.getenv("CONF_USER", "oss-automation@example.com").strip()
PAGE_ID = os.getenv("CONF_PAGE_ID", "1234567890").strip()
ENT_SLUG = os.getenv("GH_ENTERPRISE_SLUG", "example-enterprise").strip()
GH_ORG = os.getenv("GH_ORG", "example-org").strip()
CONF_SITE = os.getenv("CONF_SITE", "example-company").strip()
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
VERIFY_SSL = os.getenv("VERIFY_SSL", "true").strip().lower() not in ("false", "0", "no")
# ===================================================


def fail(message, code=1):
    print(f"[ERROR] {message}")
    sys.exit(code)


missing_required = []
if not GH_TOKEN:
    missing_required.append("GH_TOKEN")
if not CONF_TOKEN:
    missing_required.append("CONF_TOKEN")
if not CONF_USER:
    missing_required.append("CONF_USER")

if missing_required:
    fail(f"필수 환경변수 누락: {', '.join(missing_required)}", 2)

# 1. GitHub Enterprise GraphQL API를 이용해 Enterprise 전체 멤버(67명) 마스터 추출
print("[INFO] 1. GitHub Enterprise GraphQL API 호출 중 (Unaffiliated 포함 전체 수집)...")
url = "https://api.github.com/graphql"
headers = {"Authorization": f"Bearer {GH_TOKEN}"}

query = """
query($slug: String!, $endCursor: String) {
  enterprise(slug: $slug) {
    members(first: 100, after: $endCursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        ... on EnterpriseUserAccount {
          login
          name
          organizations(first: 10) {
            nodes {
              name
            }
          }
        }
      }
    }
  }
}
"""

all_members = []
variables = {"slug": ENT_SLUG, "endCursor": None}

while True:
    response = requests.post(
        url,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        fail(f"GitHub GraphQL 호출 실패 (상태코드: {response.status_code})\n{response.text}")
        
    res_data = response.json()
    members_data = res_data.get('data', {}).get('enterprise', {}).get('members', {})
    nodes = members_data.get('nodes', [])
    all_members.extend(nodes)
    
    page_info = members_data.get('pageInfo', {})
    if not page_info.get('hasNextPage'):
        break
    variables["endCursor"] = page_info.get('endCursor')

print(f"[INFO] GitHub Enterprise 전체 등록 인원 {len(all_members)}명 수집 완료.")
enterprise_member_logins = [member.get('login', '') for member in all_members if member.get('login', '')]
enterprise_member_set = set(enterprise_member_logins)

# 2. GitHub Copilot 실제 활성화 유저 바인딩
print("[INFO] 2. GitHub Copilot 할당 전체 명단 동기화 중...")
copilot_users = set()
cp_page = 1

while True:
    cp_url = f"https://api.github.com/enterprises/{ENT_SLUG}/copilot/billing/seats?page={cp_page}&per_page=100"
    cp_response = requests.get(
        cp_url,
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
        timeout=REQUEST_TIMEOUT,
    )
    if cp_response.status_code == 200:
        seats_data = cp_response.json().get('seats', [])
        if not seats_data:
            break
        for seat in seats_data:
            u_login = seat.get('assignee', {}).get('login')
            if u_login:
                copilot_users.add(u_login)
        cp_page += 1
    else:
        fail(f"GitHub Copilot seats 조회 실패 (상태코드: {cp_response.status_code})\n{cp_response.text}")


# 3. 데이터 통합 가공, 보유량 확정 및 HTML 테이블 생성
print("[INFO] 3. 데이터 통합 가공 및 보유량/잔여수 실시간 계산 중...")

# --- 보유 라이센스 수량 명확한 상수로 확정 고정 ---
github_total = 85 
copilot_total = 70  # 가변적인 API 필드 대신 계약 한도 수량 70으로 명확히 고정

# --- 실시간 사용수 및 잔여수 최종 정산 ---
github_count = 0
copilot_count = 0
rows_html = ""
enterprise_unaffiliated_logins = []

for idx, member in enumerate(all_members, start=1):
    github_id = member.get('login', '')
    display_name = member.get('name') if member.get('name') else github_id
    
    orgs = [o.get('name') for o in member.get('organizations', {}).get('nodes', [])]
    if not orgs and github_id:
        enterprise_unaffiliated_logins.append(github_id)
    
    # Github 라이선스 판별
    if not orgs:
        is_github = ""
    else:
        is_github = "사용중"
        github_count += 1
        
    # Copilot 라이선스 판별
    if github_id in copilot_users:
        is_copilot = "사용중"
        copilot_count += 1
    else:
        is_copilot = ""
        
    # 소속 컬럼 매핑 규칙 (examplepartners 포함 시 "partners", 그 외 "Example Organization")
    if "examplepartners" in github_id.lower():
        dept_name = "partners"
    else:
        dept_name = "Example Organization"
    
    rows_html += f"""
    <tr>
        <td>{idx}</td>
        <td>{display_name}</td>
        <td>{is_github}</td>
        <td>{is_copilot}</td>
        <td>{dept_name}</td>
    </tr>
    """

enterprise_member_only = sorted(enterprise_member_set - copilot_users)
copilot_only = sorted(copilot_users - enterprise_member_set)

print(f"[DEBUG] Enterprise member login 목록 ({len(enterprise_member_logins)}): {', '.join(sorted(enterprise_member_logins))}")
print(f"[DEBUG] Enterprise unaffiliated login 목록 ({len(enterprise_unaffiliated_logins)}): {', '.join(sorted(enterprise_unaffiliated_logins)) if enterprise_unaffiliated_logins else '(없음)'}")
print(f"[DEBUG] Copilot seat login 목록 ({len(copilot_users)}): {', '.join(sorted(copilot_users))}")
print(f"[DEBUG] Enterprise member에만 존재하는 사용자 ({len(enterprise_member_only)}): {', '.join(enterprise_member_only) if enterprise_member_only else '(없음)'}")
print(f"[DEBUG] Copilot seat에만 존재하는 사용자 ({len(copilot_only)}): {', '.join(copilot_only) if copilot_only else '(없음)'}")

# 실시간 잔여 수 계산
github_remains = github_total - github_count
copilot_remains = copilot_total - copilot_count

current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 4. Confluence 데이터 바인딩 양식
page_content = f"""
<p>회사 내부에서 보유한 Github/Copilot 라이센스의 수량과 사용자별 신청 현황을 분리해서 관리하기 위한 페이지입니다.</p>
<p style="color: gray;">시스템 자동 동기화 시간: {current_time}</p>
<h2>Github/Copilot 라이센스 현황</h2>
<h3>라이센스 수량 요약</h3>
<table data-layout="default">
    <tbody>
        <tr>
            <th>라이센스 종류</th>
            <th>보유 라이센스</th>
            <th>현재 사용 수</th>
            <th>잔여 수</th>
        </tr>
        <tr>
            <td>Github</td>
            <td>{github_total}</td>
            <td>{github_count}</td>
            <td>{github_remains}</td>
        </tr>
        <tr>
            <td>Copilot</td>
            <td>{copilot_total}</td>
            <td>{copilot_count}</td>
            <td>{copilot_remains}</td>
        </tr>
    </tbody>
</table>
<p></p>
<h3>사용자별 현황</h3>
<table data-layout="default">
    <tbody>
        <tr>
            <th>No</th>
            <th>이름</th>
            <th>Github</th>
            <th>Copilot</th>
            <th>소속</th>
        </tr>
        {rows_html}
    </tbody>
</table>
"""


# 5. Confluence API 업데이트 진행
print("[INFO] 4. Confluence 현재 페이지 상태 확인 중...")
conf_auth = HTTPBasicAuth(CONF_USER, CONF_TOKEN)
conf_headers = {"Accept": "application/json", "Content-Type": "application/json"}
conf_base_url = f"https://{CONF_SITE}.atlassian.net/wiki/api/v2/pages/{PAGE_ID}"

ver_response = requests.get(
    conf_base_url,
    headers=conf_headers,
    auth=conf_auth,
    verify=VERIFY_SSL,
    timeout=REQUEST_TIMEOUT,
)
if ver_response.status_code != 200:
    fail(f"Confluence 접근 실패 (상태코드: {ver_response.status_code})\n{ver_response.text}")

page_info = ver_response.json()
current_version = page_info["version"]["number"]
page_title = page_info["title"]

print("[INFO] 5. Confluence 페이지 최종 통합 업데이트(PUT) 요청 중...")
update_data = {
    "id": PAGE_ID, "status": "current", "title": page_title,
    "body": {"representation": "storage", "value": page_content},
    "version": {"number": current_version + 1, "message": "보유량 계산 로직 보정 및 컬럼 개편 최종 완결본"}
}

update_response = requests.put(
    conf_base_url,
    headers=conf_headers,
    auth=conf_auth,
    data=json.dumps(update_data),
    verify=VERIFY_SSL,
    timeout=REQUEST_TIMEOUT,
)

if update_response.status_code == 200:
    print(f"[SUCCESS] 동기화 완료: 총 {len(all_members)}명 정렬 완료")
    print(f"[INFO] 최종 집계 수치 [Github: {github_count}명 / Copilot: {copilot_count}명]")
    print(f"LICENSE_GITHUB_TOTAL={github_total}")
    print(f"LICENSE_GITHUB_USED={github_count}")
    print(f"LICENSE_GITHUB_REMAINS={github_remains}")
    print(f"LICENSE_COPILOT_TOTAL={copilot_total}")
    print(f"LICENSE_COPILOT_USED={copilot_count}")
    print(f"LICENSE_COPILOT_REMAINS={copilot_remains}")
else:
    fail(f"업데이트 실패: {update_response.text}")
