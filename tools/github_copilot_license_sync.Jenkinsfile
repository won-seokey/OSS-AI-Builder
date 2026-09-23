pipeline {
    agent {
        label 'windows-agent'
    }

    options {
        disableConcurrentBuilds()
    }

    parameters {
        string(
            name: 'EMAIL_RECIPIENTS',
            defaultValue: 'oss-automation@example.com',
            description: '이메일 수신자 (쉼표로 구분)'
        )
        booleanParam(
            name: 'ENABLE_EMAIL',
            defaultValue: true,
            description: '빌드 결과 이메일 발송 여부'
        )
        string(
            name: 'CONF_USER',
            defaultValue: 'oss-automation@example.com',
            description: 'Confluence 계정 이메일 (credential 미분리 정책)'
        )
        string(
            name: 'CONF_TOKEN_CREDENTIAL_ID',
            defaultValue: 'CONF_TOKEN',
            description: 'Confluence API Token credential ID'
        )
        string(
            name: 'CONF_PAGE_ID',
            defaultValue: '1234567890',
            description: 'Confluence 대상 페이지 ID'
        )
        string(
            name: 'GH_ENTERPRISE_SLUG',
            defaultValue: 'example-enterprise',
            description: 'GitHub Enterprise slug'
        )
        string(
            name: 'GH_ORG',
            defaultValue: 'example-org',
            description: 'Copilot seat 조회 대상 GitHub Organization'
        )
        string(
            name: 'CONF_SITE',
            defaultValue: 'example-company',
            description: 'Confluence 사이트 서브도메인 (예: example-company)'
        )
    }

    environment {
        SCRIPT_DIR = 'tools'
        PYTHON_SCRIPT = 'github_copilot_license_sync.py'
    }

    stages {
        stage('Verify Files') {
            steps {
                script {
                    echo "INFO: 파일 확인 중..."
                    def pythonScript = "${env.WORKSPACE}/${env.SCRIPT_DIR}/${env.PYTHON_SCRIPT}"

                    if (!fileExists(pythonScript)) {
                        error "ERROR: Python 파일을 찾을 수 없음: ${pythonScript}"
                    }

                    echo "INFO: Python 파일 확인 완료"
                }
            }
        }

        stage('Execute Python Script') {
            steps {
                script {
                    withCredentials([
                        string(credentialsId: 'JIRA-GITHUB-IP-TOKEN', variable: 'GH_TOKEN'),
                        string(credentialsId: params.CONF_TOKEN_CREDENTIAL_ID, variable: 'CONF_TOKEN')
                    ]) {
                        try {
                            echo "INFO: Python 스크립트 실행 중..."

                            def outputFile = "${env.WORKSPACE}\\script_output_${env.BUILD_NUMBER}.log"
                            def exitCode = bat(
                                script: """
                                    @echo off
                                    setlocal
                                    set "PY_CMD="
                                    set "OUTPUT_FILE=${outputFile}"

                                    py -3 -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY_CMD=py -3"
                                    if not defined PY_CMD (
                                        python -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY_CMD=python"
                                    )
                                    if not defined PY_CMD (
                                        python3 -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY_CMD=python3"
                                    )

                                    if not defined PY_CMD (
                                        echo [ERROR] Python executable not found. > "%OUTPUT_FILE%"
                                        type "%OUTPUT_FILE%"
                                        exit /b 9009
                                    )

                                    echo INFO: Using Python command: %PY_CMD%
                                    cd /d "${env.WORKSPACE}\\${env.SCRIPT_DIR}"
                                    chcp 65001 >nul
                                    set "PYTHONUTF8=1"
                                    set "PYTHONIOENCODING=utf-8"

                                    set "CONF_USER=${params.CONF_USER}"
                                    set "CONF_PAGE_ID=${params.CONF_PAGE_ID}"
                                    set "GH_ENTERPRISE_SLUG=${params.GH_ENTERPRISE_SLUG}"
                                    set "GH_ORG=${params.GH_ORG}"
                                    set "CONF_SITE=${params.CONF_SITE}"

                                    %PY_CMD% ${env.PYTHON_SCRIPT} > "%OUTPUT_FILE%" 2>&1
                                    set "PY_EXIT=%ERRORLEVEL%"

                                    type "%OUTPUT_FILE%"
                                    exit /b %PY_EXIT%
                                """,
                                returnStatus: true
                            )

                            def output = fileExists(outputFile)
                                ? readFile(file: outputFile, encoding: 'UTF-8').trim()
                                : "(출력 없음)"

                            env.SCRIPT_OUTPUT = output
                            echo output

                            def parseLicenseSummary = { text ->
                                def values = [:]
                                (text ?: '').eachLine { line ->
                                    if (line.startsWith('LICENSE_') && line.contains('=')) {
                                        def parts = line.split('=', 2)
                                        values[parts[0]] = parts[1]
                                    }
                                }

                                if (!values) {
                                    return ''
                                }

                                return """
                                    <div class=\"section\">
                                        <div class=\"section-title\">라이선스 요약</div>
                                        <table class=\"info-table\">
                                            <tr>
                                                <td>GitHub</td>
                                                <td>${values.get('LICENSE_GITHUB_USED', '-')}/${values.get('LICENSE_GITHUB_TOTAL', '-')} (잔여 ${values.get('LICENSE_GITHUB_REMAINS', '-')})</td>
                                            </tr>
                                            <tr>
                                                <td>Copilot</td>
                                                <td>${values.get('LICENSE_COPILOT_USED', '-')}/${values.get('LICENSE_COPILOT_TOTAL', '-')} (잔여 ${values.get('LICENSE_COPILOT_REMAINS', '-')})</td>
                                            </tr>
                                        </table>
                                    </div>
                                """.stripIndent().trim()
                            }

                            env.CUSTOM_SECTION = parseLicenseSummary(output)

                            if (exitCode != 0) {
                                error "ERROR: Python 스크립트 실패 (exit code: ${exitCode})"
                            }

                            env.BUILD_STATUS = "SUCCESS"
                            currentBuild.result = 'SUCCESS'

                        } catch (Exception e) {
                            echo "ERROR: 스크립트 실행 중 오류 발생"
                            env.BUILD_STATUS = "FAILED"
                            env.ERROR_MESSAGE = e.message
                            env.SCRIPT_OUTPUT = e.message
                            env.CUSTOM_SECTION = ''
                            currentBuild.result = 'FAILURE'
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            script {
                if (!params.ENABLE_EMAIL) {
                    echo "INFO: ENABLE_EMAIL=false 설정으로 이메일 발송을 건너뜁니다."
                    return
                }

                echo "INFO: 이메일 전송 준비 중..."

                def escapeHtml = { text ->
                    (text ?: '')
                        .replace('&', '&amp;')
                        .replace('<', '&lt;')
                        .replace('>', '&gt;')
                }
                def emailPattern = ~/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/

                def statusClass = currentBuild.result == 'SUCCESS' ? 'status-success' : 'status-failure'
                def statusText = currentBuild.result == 'SUCCESS' ? '성공' : '실패'
                def mailTitle = 'GitHub/Copilot 라이선스 현황 최신화 작업 결과'
                def buildTime = new Date().format('yyyy-MM-dd HH:mm:ss z', TimeZone.getTimeZone('Asia/Seoul'))
                def scriptOutputEscaped = escapeHtml(env.SCRIPT_OUTPUT ?: '(출력 없음)')
                def errorMessageEscaped = escapeHtml(env.ERROR_MESSAGE ?: 'Unknown error')
                def errorSection = currentBuild.result != 'SUCCESS'
                    ? """
                      <div class="section">
                          <div class="section-title">오류 정보</div>
                          <div class="error-box">${errorMessageEscaped}</div>
                      </div>
                      """
                    : ''
                def fallbackEmailBody = """
                    <!DOCTYPE html>
                    <html>
                    <body>
                        <h2>GitHub Copilot License Sync 작업 결과</h2>
                        <p><strong>상태:</strong> ${statusText}</p>
                        <p><strong>Job 이름:</strong> ${escapeHtml(env.JOB_NAME ?: '')}</p>
                        <p><strong>Build 번호:</strong> #${escapeHtml(env.BUILD_NUMBER ?: '')}</p>
                        <p><strong>실행 시간:</strong> ${buildTime}</p>
                        <p><strong>Jenkins URL:</strong> <a href="${env.BUILD_URL ?: ''}">${env.BUILD_URL ?: ''}</a></p>
                        <h3>실행 결과</h3>
                        <pre>${scriptOutputEscaped}</pre>
                        ${currentBuild.result != 'SUCCESS' ? "<h3>오류 정보</h3><pre>${errorMessageEscaped}</pre>" : ''}
                        ${env.CUSTOM_SECTION ?: ''}
                    </body>
                    </html>
                """

                def emailTemplatePath = "${env.WORKSPACE}/${env.SCRIPT_DIR}/email_template.html"
                def emailBody
                if (fileExists(emailTemplatePath)) {
                    emailBody = readFile(file: emailTemplatePath)
                        .replace('{{MAIL_TITLE}}', mailTitle)
                        .replace('{{STATUS_CLASS}}', statusClass)
                        .replace('{{STATUS_TEXT}}', statusText)
                        .replace('{{JOB_NAME}}', env.JOB_NAME ?: '')
                        .replace('{{BUILD_NUMBER}}', env.BUILD_NUMBER ?: '')
                        .replace('{{BUILD_TIME}}', buildTime)
                        .replace('{{BUILD_URL}}', env.BUILD_URL ?: '')
                        .replace('{{SCRIPT_OUTPUT}}', scriptOutputEscaped)
                        .replace('{{ERROR_SECTION}}', errorSection)
                        .replace('{{BUILD_CONSOLE_URL}}', "${env.BUILD_URL}console")
                        .replace('{{CUSTOM_SECTION}}', env.CUSTOM_SECTION ?: '')
                } else {
                    echo "WARN: 이메일 템플릿 파일이 없어 fallback 본문을 사용합니다: ${emailTemplatePath}"
                    emailBody = fallbackEmailBody
                }

                def emailSubject = "[${currentBuild.result}] GitHub Copilot License Sync - Build #${env.BUILD_NUMBER}"
                def rawRecipients = (params.EMAIL_RECIPIENTS ?: '').trim()

                def parsedRecipients = rawRecipients
                    ? rawRecipients.split(',').collect { it.trim() }.findAll { it }
                    : []
                def validRecipients = parsedRecipients.findAll { it ==~ emailPattern }
                def invalidRecipients = parsedRecipients.findAll { !(it ==~ emailPattern) }

                if (invalidRecipients) {
                    echo "WARN: 잘못된 이메일 주소를 제외합니다: ${invalidRecipients.join(', ')}"
                }

                if (!validRecipients) {
                    echo "WARN: EMAIL_RECIPIENTS가 비어 있어 이메일 전송을 건너뜁니다."
                    return
                }

                def toRecipients = validRecipients.join(',')

                try {
                    emailext(
                        to: toRecipients,
                        subject: emailSubject,
                        body: emailBody,
                        mimeType: 'text/html'
                    )
                    echo "INFO: 이메일 전송 완료: ${toRecipients}"
                } catch (Exception e) {
                    echo "WARN: 이메일 전송 실패: ${e.message}"
                }
            }
        }

        success {
            echo "INFO: 작업 완료 (SUCCESS)"
        }

        failure {
            echo "ERROR: 작업 완료 (FAILURE)"
        }
    }
}
