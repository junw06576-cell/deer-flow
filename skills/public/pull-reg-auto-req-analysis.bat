@echo off
rem ============================================================
rem  pull-reg-auto-req-analysis.bat
rem  Pull reg-auto-req-analysis skill from TFS to local.
rem  - dir is a git repo      -> git pull
rem  - dir does not exist     -> git clone
rem  - dir exists (non-git)   -> show hint, do nothing
rem  Change PAT: update AUTH below (echo :YourPAT | base64)
rem ============================================================

set "DIR=%~dp0reg-auto-req-analysis"
set "URL=http://tfs2018-web.winning.com.cn:8080/tfs/WinCode/Skill/_git/reg-auto-req-analysis"
set "AUTH=OmRjZnd4dm5idGc2NmxrZHRlenVzNWw3N3lycno1ZnFuemxzbXd3aGkybjNocDdpemF2bWE="

if exist "%DIR%\.git" (
  git -C "%DIR%" -c "http.extraHeader=AUTHORIZATION: Basic %AUTH%" pull origin master
) else if not exist "%DIR%" (
  git -c "http.extraHeader=AUTHORIZATION: Basic %AUTH%" clone "%URL%" "%DIR%"
) else (
  echo [ERROR] "%DIR%" exists but is not a git repo.
  echo         Backup and remove it first, then rerun this script.
)

pause
