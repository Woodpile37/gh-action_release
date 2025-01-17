from datetime import datetime, timezone
import json
import polling
import requests
from dryable import Dryable
from polling import TimeoutException
from requests.auth import HTTPBasicAuth
from requests.models import Response
from release.steps.ReleaseRequest import ReleaseRequest


class ReleasabilityFailure(Exception):
    def __init__(self, releasability):
        def format_failed_releasability(releasability):
            metadata = json.loads(releasability['metadata'])

            def format_ra_check(check):
                state = check['state']
                # \u2705 is "white heavy check mark", \u274c is "cross mark"
                status_char = '\u2705' if state == 'PASSED' else '\u274c'
                reason = f" - {check.get('message', '')}" if state != 'PASSED' else ''
                return f"* {status_char} {check['name']}: {state}{reason}"

            return '\n'.join([format_ra_check(check) for check in metadata['checks'] if check['state'] != 'NOT_RELEVANT'])
        super(Exception, self).__init__(format_failed_releasability(releasability))


class Burgr:
    url: str
    auth_header: HTTPBasicAuth
    release_request: ReleaseRequest

    def __init__(self, url, user, password, release_request):
        self.url = url
        self.auth_header = HTTPBasicAuth(user, password)
        self.release_request = release_request
        version = self.release_request.version
        # SLVSCODE-specific
        if self.release_request.project == 'sonarlint-vscode':
            version = version.split('+')[0]
        self.version = version

    # This will only work for a branch build, not a PR build
    # because a PR build notification needs `"pr_number": NUMBER` instead of `'branch': NAME`
    @Dryable(logging_msg='{function}()')
    def notify(self, status):
        payload = {
            'repository': f"{self.release_request.org}/{self.release_request.project}",
            'pipeline': self.release_request.buildnumber,
            'name': 'RELEASE',
            'system': 'github',
            'type': 'release',
            'number': self.release_request.buildnumber,
            'branch': self.release_request.branch,
            'sha1': self.release_request.sha,
            'url': f"https://github.com/{self.release_request.org}/{self.release_request.project}/releases",
            'status': status,
            'metadata': '',
            'started_at': datetime.now(timezone.utc).astimezone().isoformat(),
            'finished_at': datetime.now(timezone.utc).astimezone().isoformat()
        }
        print(f"burgr payload:{payload}")
        url = f"{self.url}/api/stage"
        r = requests.post(url, json=payload, auth=self.auth_header)
        if r.status_code != 201:
            print(f"burgr notification failed code:{r.status_code}")

    @Dryable(logging_msg='{function}()')
    def start_releasability_checks(self):
        print(f"Starting releasability check: {self.release_request.project}#{self.version}")

        url = f"{self.url}/api/project/SonarSource/{self.release_request.project}/releasability/start/{self.version}"
        response = requests.post(url, auth=self.auth_header)
        # Throw proper exception on 4xx or 5xx before attempting to consume response body.
        response.raise_for_status()
        message = json.loads(response.text).get('message', '')
        if response.status_code == 200 and message == "done":
            print(f"Releasability checks started successfully for {self.version} {self.release_request.branch}")
        else:
            print(f"Releasability checks failed to start: {response} '{message}'")
            raise Exception(f"Releasability checks failed to start: '{message}'")

    @Dryable(logging_msg='{function}()')
    def get_releasability_status(self,
                                 nb_of_commits: int = 5,
                                 step: int = 4,
                                 timeout: int = 300,
                                 check_releasable: bool = True) -> bool:
        r"""Get Burgrx for latest releasability status (polls until all checks have completed.)

        :param nb_of_commits: number of latest commits on the branch to get the status from.
        :param step: step in seconds between polls. (For testing, otherwise use default value)
        :param timeout: timeout in seconds for attempting to get status. (For testing, otherwise use default value)
        :param check_releasable: whether should check for 'releasable' flag in json response. (For testing, otherwise use default value)
        :return: Metadata containing detailed releasability information, False if an unexpected error occurred.
        """

        url = f"{self.url}/api/commitPipelinesStages"
        url_params = {
            "project": f"{self.release_request.org}/{self.release_request.project}",
            "branch": self.release_request.branch,
            "nbOfCommits": nb_of_commits,
            "startAtCommit": 0
        }
        try:
            releasability = polling.poll(
                lambda: self._get_latest_releasability_stage(
                    requests.get(url, params=url_params, auth=self.auth_header),
                    check_releasable
                ),
                step=step,
                timeout=timeout)
            status = releasability['status']
            print(f"Releasability checks finished with status '{status}'")
            if status != 'passed':
                raise ReleasabilityFailure(releasability)
            return releasability.get('metadata')
        except TimeoutException:
            print("Releasability timed out")
            raise Exception("Releasability timed out")
        except Exception as e:
            print(f"Cannot complete releasability checks:", e)
            raise e

    def _get_latest_releasability_stage(self, response: Response, check_releasable: bool = True) -> bool:
        print(f"Polling releasability status... {response.url}")

        if response.status_code != 200:
            raise Exception(f"Error occurred while trying to retrieve current releasability status: "
                            f"({response.status_code}) {response.text}")

        commits_info = response.json()
        if len(commits_info) == 0:
            raise Exception(f"No commit information found in burgrx for this branch")

        def get_corresponding_pipeline():
            for commit_info in commits_info:
                pipelines = commit_info.get('pipelines') or []
                it = next((x for x in pipelines if x.get('version') == self.version), None)
                if it is not None:
                    return it
            return None
        pipeline = get_corresponding_pipeline()
        if not pipeline:
            raise Exception(f"No pipeline info found for version '{self.version}'")

        if check_releasable and not pipeline.get('releasable'):
            raise Exception(f"Pipeline '{pipeline}' is not releasable")

        stages = pipeline.get('stages') or []
        latest_releasability_stage = next(
            (stage for stage in reversed(stages) if stage.get('type') == 'releasability'),
            None
        )

        def is_finished(status: str):
            return status == 'errored' or status == 'failed' or status == 'passed'
        if latest_releasability_stage and is_finished(latest_releasability_stage['status']):
            return latest_releasability_stage

        print("Releasability checks still running")
        return False
