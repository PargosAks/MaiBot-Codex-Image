# ���������� MCP ����Linux / Docker��

�״ΰ�װ���ȿ� [���˿��ٿ�ʼ](quickstart.md)������Դ��Ž����Ŀ¼����������ִ�� `bash setup.sh`��Linux root Ĭ������ systemd������밲װ����Ϊ 0.6.3������������Ϊ 0.5.0����������������������� MCP���ű���Ҫ�������û���װ�ú��� MaiBot ���������ð����������µ� 1��7 ���ǿ�ѡ���ֶ����𼰾ɷ��벼�ֹ������̣��������û��ıؾ����衣���߲��Զ���¼������ MaiBot ��Ȩ��������� WebUI ��Ȩ�����ء����� Release������ VPS ��ȷ������������������Docker ��ͨ����ʵ��ͼ��ͼ�Դ���֤��Windows �� [�ֶ�ָ��](manual-setup.md)��

**���� `setup.sh` ��װ���û���Ҫ�յ� 3��4��7 ���������á���Կ�򸲸Ƿ����ļ���** Linux root �°�װĬ��ʹ������·�������а�װ�� `--data-dir` ָ����Ŀ¼ʱ����ԭ����Ϊ׼��

| ���� | `setup.sh` Ĭ�ϲ��� |
| --- | --- |
| Դ�� | MaiBot ʵ�ʲ��Ŀ¼������ VPS Ϊ `/opt/maibot/plugins/codex_image` |
| �������á���Կ | `/var/lib/maibot-codex-image/service.toml`��`secret.txt` |
| ������ͼƬ | `/var/lib/maibot-codex-image/runtime/state` |
| ���񻷾� | `/var/lib/maibot-codex-image/venv` |
| ���ɵ� unit / �Ѱ�װ�� unit | `/var/lib/maibot-codex-image/runtime/maibot-codex-image.service` / `/etc/systemd/system/maibot-codex-image.service` |

������������� [����״̬����־](#����������״̬����־)���������ɷ��ʶ�������ʱ�� [Docker �������ӳ�ʱ](#docker-�������ӳ�ʱ)��root ������uv `.lock`��systemd ��װ����QQ ��Ȩ��δ֪����Ĵ����� [�����Ų�](troubleshooting.md)��

0.5.0 �������û����ڲ�� `connection.allowed_users` ���ã�������ά������������ `service.toml` �� `allowed_users` ��ȡʱ�ᾯ�沢���ԣ�����ɾ�����������У�� Bearer��HMAC����Ч�ڡ�scope �������������������Կ���÷���Ϊ���š�

0.4.0 ͳһʹ�� external-only �ܹ���MaiBot ����ֱ�Ӳ����������� Docker �У���������ӵ������е������� `service/`��

```text
MaiBot ��� �� ������ MCP ���� �� ������ Codex
```

���������� Codex ��¼�������ͼƬ�����ͨ�� MCP ȡ��ͼƬ�����蹲��ͼƬĿ¼�������޸� MaiBot ���塢Dockerfile��Compose������Ҳ������ MaiBot ������װ Codex������ƾ֤������ Docker ����Ȩ�ޡ�

����Ϊ�ֶ����벼�ֵ��°�װʾ����Ŀ¼��ʵ���û���Ȩ�޵���������ʾ�������ú���Կ�����ֶ��ֿ��ڣ����ϱ��� `setup.sh` Ĭ������Ŀ¼��ͬ������ʱ�ȿ� [Ǩ��˵��](advanced.md#�Ӿɰ汾Ǩ��)����Ҫ����ԭ�������á���Կ�����ݡ�

## 1. ׼��Դ�루�ֶ����벼�֣�

��������Ҫ Git��uv��Python 3.12+ ���Ѱ�װ�Ĺٷ� Codex CLI��ʹ�üƻ����� MCP ��ϵͳ�û�ִ�У�

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git ~/maibot-codex-image-host
cd ~/maibot-codex-image-host
uv sync --project service --no-dev
codex login status
```

���� ChatGPT ��¼�������µ�¼��δ��¼ʱ�ֶ�ִ�� `codex login --device-auth`���������õ� `home` ����ʱ��������û���ǰ�� `CODEX_HOME`��δ����ʱʹ�� `~/.codex`��������ƾ֤����ɾ���ճ� Codex ���ã�ר�õ�¼Ŀ¼��ѡ��

�� MaiBot ʵ��ʹ�õĲ���־û�Ŀ¼���а�װ�����

```sh
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git /���MaiBotʵ��/plugins/codex_image
```

��Ŀ¼����ͨ�����л��ƹ� MaiBot ���֣���Ҫ��������������Ŀ¼��¡��ͼٶ���������ء������������װ�� MaiBot ʵ��ʹ�õĻ������������ֿ��Ŀ¼ `_manifest.json` �� `requirements.txt`��

ֱ�Ӳ���� MaiBot ���� MCP ��ͬ�����ֿ�ɱ������û����ʣ���ʡ�Եڶ��ݲֿ⣬���� `plugins/codex_image/service/`���Ե�������������������Ӧ����Դ��Ŀ¼�⣬����Դ�벻�������������ݱ��ݡ�

## 2. ѡ�����ɴ�ļ�����ַ

MaiBot ֱ�������������������������� host ����ʱ������ʹ�� `127.0.0.1`��

Linux Docker Engine bridge �����£���ֻ���鿴ʵ��������������Ϣ��`maibot` �滻Ϊ��������

```sh
docker inspect --type=container maibot --format '{{json .NetworkSettings.Networks}}'
```

ѡȡ������ʵ����������������� `Gateway`������ `172.18.0.1`��**����� `host` ���� URL ��ʹ��ͬһ��ʵ�� Gateway ��ַ�Ͷ˿ڡ�** ��Ҫ�ճ�ʾ�� IP����Ҫ�������� `127.0.0.1` ������������

��ǰ�����У�� HTTP Host����������Ϊ `0.0.0.0` ������������ͨ������ IP ���������ʡ�����ָ��ֱ�Ӱ�ѡ�� Gateway����ʹ���������� Host ƥ�䡣����Ҫ����ǽ���У����Ƚ�������ǰ MaiBot ���� IP ���ʸ� Gateway �ķ���˿ڣ��� [Docker �������ӳ�ʱ](#docker-�������ӳ�ʱ)������˿�ӳ��򿪷Ź����˿ڣ����������ص�ַ�²�������

��ָ�ϲ�Ҫ��� Compose ������ `host.docker.internal` ӳ�䡣Docker Desktop��rootless�������������ŵ�ַ���������绷�������к˶Կɴ��ַ�� Host У�飬����ֱ�����ô�����

## 3. ��������������

���������ֿ��Ŀ¼��������������ʱ����ģ�壺

```sh
cp -n service/config.example.toml service.toml
```

�༭ `service.toml`������Ϊ Docker bridge ʾ�����־û�·���� Gateway �����滻��`binary/home` ����ʹ���Զ����������е�¼��

```toml
binary = ""
home = ""
state = "/��ĳ־û�Ŀ¼/maibot-image/state"
model = ""
host = "172.18.0.1"
port = 8765
```

- `binary` ʡ�Ի�����ʱ�� PATH ����ԭ�� Codex���Ҳ���ʱ��дʵ��ԭ����ִ�г���·����`command -v codex` ���ṩ�����������������ű�������ȷ�϶�Ӧ��ԭ������
- `home` ʡ�Ի�����ʱ����ʹ�÷�����̵� `CODEX_HOME`������ʹ�ø��û��� `~/.codex`����ʽ·�����ȣ�����ʱ��������ֵ����Ҫ�� TOML ����д������ `$CODEX_HOME`��������չ����
- `state` ʹ�ÿ�д�ľ���·����������������ݿ⡢����͸����ͼĿ¼�����������
- ֱ�Ӳ���ʱ�� `host` ��Ϊ `127.0.0.1`�����Ҳ����Ӧ��ַ��

���������ֶμ�������ģ���������ã��� [��������˵��](advanced.md#��������������)��

## 4. �ṩ��Կ������

���������ֿ��Ŀ¼�״����������Կ�������ļ�ʱ���ã�

```sh
umask 077
if [ ! -f secret.txt ]; then
  ./service/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(40))' > secret.txt
fi
export CODEX_IMAGE_SECRET="$(cat secret.txt)"
uv run --project service --no-dev codex-image-mcp --config "$PWD/service.toml"
```

`CODEX_IMAGE_SECRET` ���� 32 �ַ����ɻ���������ȡ����д�� `service.toml`�����ܺ�ͬһ����Կ�ļ�����Ҫÿ��������������Կ��Ҳ��Ҫ�������빫����־���ύ�� Git��

Ҳ�ɲ����� `CODEX_IMAGE_SECRET`������ `CODEX_IMAGE_SECRET_FILE` Ϊ����Կ�ļ��ľ���·��������ͬʱ����ʱ��ֱ����Կ�������ȣ���ֵҲ������˵��ļ�����Կ�ļ�ӦΪ UTF-8�����ܰ���Կ��������ļ�·��������

ǰ̨�رռ�ֹͣ���񡣺�̨���кͿ��������� [systemd ����](#7-systemd-��̨�����뿪������)��ÿ�� `state` Ŀ¼ֻ����һ������ʵ����MaiBot ���������������Ϊ���������� MCP��

## 5. ��Բ��

�� MaiBot ������û��� `config.toml` ����д��

```toml
[plugin]
config_version = "0.6.3"
enabled = true

[connection]
url = "http://172.18.0.1:8765/mcp"
secret = "��д������ secret.txt �е�������Կ"
allowed_users = ["qq:123456789"]
poll_seconds = 2
watch_seconds = 360
```

���������Ĳ�����÷�Χ���������÷���ģʽ�򱾵� Codex��ֱ�Ӳ����ҷ������ `127.0.0.1` ʱ����Ӧ�޸� URL������������û������ `codex_image_command` ������֡�ͼƬ����Ȩ�ޣ����沢���¼��ز����

## 6. �ֲ���֤

1. �������Է����û�ִ�� `codex login status`��ȷ������ʹ�õĵ�¼Ŀ¼����� `home` һ�¡�
2. �� MaiBot ��ִ�� `/��ͼ���`����� MCP ��������Ȩ��������� Codex ��¼����������ͼ����
3. ��Ҫ������ʵ��ͼʱ�����ύһ�� `/��ͼ һ����ɫ�����Σ���ɫ����`����¼�����ţ����״̬���Զ���ͼ��ʵ�������е�ͼƬ���˲����ʹ�ö��Ķ�ȡ�

�Ų���������ʱ���������������� Python ����һ�β�����Կ�����󣬵�ַ����ʵ�� Gateway��

```sh
docker exec maibot python3 -c 'import http.client; c=http.client.HTTPConnection("172.18.0.1",8765,timeout=5); c.request("GET","/mcp"); print(c.getresponse().status)'
```

���� `401` ��˵���пɴ�ļ�Ȩ�˵�ܾ���δ��֤����Bearer У������ MCP Host ��飬����⻹����֤�� Host ƥ�䡢ǩ�����󡢵�¼���ͼ�ɹ������Ӿܾ�ʱ������ͼ�����ַ����ʱ������������ǽ������֤��������� `403` ʱ�˶� URL �� Host ��������á�

��������ʱ�ľ���ֲ���� [Docker �������ӳ�ʱ](#docker-�������ӳ�ʱ)��

## 7. systemd ��̨�����뿪������

���ڽ������ֶ����벼�֡�`setup.sh` �ѹ���ͬ������ʱ������ [����״̬����־](#����������״̬����־)����Ҫ������ unit���ֶ����������ǰ����������ֶ����ԣ���������ͨ Linux systemd��root �û����ֿ�λ�� `/opt/maibot-codex-image-host` Ϊ������������ `service.toml`��`secret.txt` ������Ŀ¼��������������Կ��

��ǰ��ʹ�õ��� `~/maibot-codex-image-host`�����������з���Ŀ¼�滻Ϊʵ�ʾ���·�������ذ�Ǩ�ֿ�����ݡ���������ϵͳ�û���¼ Codex������ `User`��`Group`��`HOME`��`PATH` ���ļ�����Ȩ��ҲҪ��Ӧ������Docker ����˳������������ϵͳ�� Docker����ֱ������ rootless Docker��

### 7.1 ֹͣǰ̨ʵ�����̶��������¼Ŀ¼

�ȵȴ����ڻ�ͼ�������������ԭǰ̨���ڰ� `Ctrl+C`����Ҫ��ǰ̨ʵ���� systemd ͬʱʹ��ͬһ�� `state`��

ʹ���ѵ�¼ Codex �� root �û�ִ�У�

```sh
cd /opt/maibot-codex-image-host
uv sync --project service --locked --no-dev --python 3.13

unset CODEX_IMAGE_SECRET
export CODEX_IMAGE_SECRET_FILE="$PWD/secret.txt"

uv run --project service --locked --no-dev python - <<'PY'
import json
from pathlib import Path
from codex_image_mcp.config import Settings

settings = Settings.load(Path("service.toml"))
print("binary = " + json.dumps(settings.binary))
print("home = " + json.dumps(str(settings.home)))
PY
```

��ֻ�������ò���ʾ·��������¼������ͼƬ���༭ `service.toml`���� `binary`��`home` �滻������ľ���·�������������ֶΣ���Ҫ�ظ�����ͬ���ֶΡ����� npm/nvm ��װ�� Codex ��ר�õ�¼Ŀ¼Ҳ������ SSH �ն˵� PATH �� CODEX_HOME��

```sh
nano service.toml
chmod 600 secret.txt service.toml
```

### 7.2 ���������ļ�

��ͬһ�� SSH ���ڻ�ȡ uv ��ʵ��·����ȷ�����һ�м��ɹ��������

```sh
UV_BIN="$(command -v uv)"
printf 'uv ·����%s\n' "$UV_BIN"
test -n "$UV_BIN" && test -x "$UV_BIN"
```

`UV_BIN` ӦΪ���Կ�ִ���ļ�·�������� `/root/.local/bin/uv`�������� shell ��������������δ�����ŵ� `EOF` ���ڴ����ļ�ʱ�Ѹ�·��д�� `ExecStart`��systemd ����̳���� shell ������

����ͬ������ʱ�ȱ��ݣ�Ȼ��д�����ã�

```sh
if [ -f /etc/systemd/system/maibot-codex-image.service ]; then
  cp -a /etc/systemd/system/maibot-codex-image.service \
    "/etc/systemd/system/maibot-codex-image.service.bak.$(date +%Y%m%d-%H%M%S)"
fi

cat > /etc/systemd/system/maibot-codex-image.service <<EOF
[Unit]
Description=MaiBot Codex Image MCP Service
Wants=network-online.target docker.service
After=network-online.target docker.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/maibot-codex-image-host

Environment="HOME=/root"
Environment="PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="CODEX_IMAGE_SECRET_FILE=/opt/maibot-codex-image-host/secret.txt"
UnsetEnvironment=CODEX_IMAGE_SECRET

ExecStart="$UV_BIN" run --project /opt/maibot-codex-image-host/service --locked --no-dev codex-image-mcp --config /opt/maibot-codex-image-host/service.toml

Restart=on-failure
RestartSec=5
TimeoutStopSec=90
KillMode=mixed
UMask=0077

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

- `CODEX_IMAGE_SECRET_FILE` ֻ��¼��Կ�ļ�·����������Կ����д�������ļ���`UnsetEnvironment` ����ɵ�ֱ����Կ������������
- `Restart=on-failure` ���쳣�˳���ȴ� 5 ���������ֶ� `systemctl stop` �������Զ�������
- `KillMode=mixed` ��֪ͨ������ֹͣ��uv �� Unix ��ת��ֹͣ�źţ����ȴ� 90 �������ʣ����̡�
- `After` �涨����˳�򣬲���֤ Docker ���ػ� Codex �Ѿ����á�������δ����ʱ�鿴ʧ����־�������쳣�˳��ᰴ���������ٳ��ԡ�

�� MaiBot ֱ�Ӳ����Ҳ�ʹ�� Docker��ɾ�� `Wants`��`After` �е� `docker.service`������ `network-online.target`�����ǰ̨���������������Զ��� CA ����������Ҳ���ڷ�������ʽ����ʵ��ֵ��systemd �����Զ��̳� SSH �Ự��������Ҫ�Ѻ�ƾ�ݵ�ֵ����������־��

### 7.3 ��鲢���ÿ�������

�ȼ�����ã�����������������������

```sh
systemd-analyze verify /etc/systemd/system/maibot-codex-image.service
```

���ͨ����ִ�У�

```sh
systemctl daemon-reload
systemctl enable --now maibot-codex-image.service
systemctl status maibot-codex-image.service --no-pager -l
systemctl is-active maibot-codex-image.service
systemctl is-enabled maibot-codex-image.service
```

������Ԥ�ڷֱ�Ϊ `active`��`enabled`���˺�Ͽ� SSH ����ֹͣ��������Ϊ���������������� VPS����ʵ�����ָ������´��������飬���ܽ�ƾ `enabled` ��Ϊ����֤��

### 7.4 ��־���ճ�����

```sh
# ʵʱ��־��Ctrl+C ֻ�˳���־�鿴��
journalctl -u maibot-codex-image.service -f

# ��� 100 �С�
journalctl -u maibot-codex-image.service -n 100 --no-pager

# ����ִ������һ����
systemctl restart maibot-codex-image.service
systemctl stop maibot-codex-image.service
systemctl start maibot-codex-image.service
```

�޸� systemd �����ļ����� `systemctl daemon-reload` ����������ֻ�޸� `service.toml` ����Կ�ļ�����ʱ�������񼴿ɣ����˹�����Կ���뱣��һ�¡�

ȡ������������ֹͣ����ɾ�����á���¼������

```sh
systemctl disable --now maibot-codex-image.service
```

�л�������������ѡ��û�л�ͼ����ʱ�����δ֪�����񲻻��Զ��ػ���`active` ����ʾ�������У����谴�� 6 �ڼ�� `/��ͼ���` ��ʵ��������ͼ��

systemd ��Ϊ�ο���[��������������](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)��[ִ�л���](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)��[����ֹͣ��ʽ](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)��[uv �źŴ���](https://docs.astral.sh/uv/concepts/projects/run/#signal-handling)��

## ����������״̬����־

����Ϊ�������ϵ�ֻ����飬ͬʱ�����ڽű���װ���ֶ�����

```sh
systemctl status maibot-codex-image.service --no-pager --full
systemct…2197 tokens truncated…n��Ҫ Git��uv��Python 3.12+ ���Ѱ�װ�Ĺٷ� Codex CLI��ʹ�ý����� MCP ��ϵͳ�û����� PowerShell ��ִ�У�·�������Լ���λ�ã�

```powershell
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git 'D:\MaiBot\plugins\codex_image'
git clone https://github.com/PargosAks/MaiBot-Codex-Image.git 'D:\MaiBotImageService'
Set-Location 'D:\MaiBotImageService'
uv sync --project service --no-dev
codex login status
```

���� ChatGPT ��¼�������µ�¼��δ��¼���ֶ�ִ�� `codex login --device-auth`������������谲װ�� MaiBot ��ʵ�ʻ���������� `_manifest.json` �� `requirements.txt`��

ͬ�������ֻ��¡���һ�ݣ���Ϊ���� `D:\MaiBot\plugins\codex_image` �������е� `service/`���������·�����Ը�Ŀ¼Ϊ׼���������Ƕ������̣��������ݷ���Դ��Ŀ¼�⡣

## 2. ���÷���

����ѡ�ֿ��Ŀ¼��������������ʱ����ģ�壺

```powershell
if (-not (Test-Path -LiteralPath '.\service.toml')) {
    Copy-Item -LiteralPath '.\service\config.example.toml' -Destination '.\service.toml'
}
```

�༭ `service.toml`��Windows �� TOML ·������ʹ����б�ߣ�

```toml
binary = ""
home = ""
state = "D:/MaiBotImageData/state"
model = ""
host = "127.0.0.1"
port = 8765
```

`binary` ʡ�Ի�����ʱ�Զ�����ԭ�� Codex���Ҳ���ʱ���� `Get-Command codex.exe` ��ѯ������дʵ��ԭ������·������Ҫ��д `.cmd` �� `.ps1` �����ű���`home` ʡ�Ի�����ʱ����ʹ�÷�����̵� `CODEX_HOME`��δ������ѡ���û�Ŀ¼�µ� `.codex`����ʽ·�����ȣ�����ʱ��������ֵ��TOML ����չ�������� `$env:CODEX_HOME`��

����˲��������û����������� `service.toml` �е� `allowed_users` ��ȡʱ�ᾯ�沢���ԣ�����ɾ����������У�� Bearer��HMAC ǩ������Ч�ڡ�scope �������������������Կ���÷���Ϊ���š�

ר�õ�¼Ŀ¼��ѡ����ʹ�����������ֶ���¼���ն����ö�Ӧ `CODEX_HOME`���ٽ�ͬһ·������ `home`��������ƾ֤�������ǻ�ɾ��ԭ Codex `config.toml`�������������ü� [����˵��](advanced.md#��������������)��

## 3. ������Կ������

�ڲֿ��Ŀ¼�״����ɹ�����Կ���ļ�����ʱ���ã����Ƹ��ļ�ֻ�����з�����û����ʣ�

```powershell
if (-not (Test-Path -LiteralPath '.\secret.txt')) {
    $imageSecret = & '.\service\.venv\Scripts\python.exe' -c 'import secrets; print(secrets.token_urlsafe(40))'
    Set-Content -LiteralPath '.\secret.txt' -Value $imageSecret -Encoding ascii
}
$env:CODEX_IMAGE_SECRET = (Get-Content -LiteralPath '.\secret.txt' -Raw).Trim()
$imageConfig = (Resolve-Path -LiteralPath '.\service.toml').Path
uv run --project service --no-dev codex-image-mcp --config $imageConfig
```

��Կ���� 32 �ַ���ֻͨ�����������ṩ�����񣬲�д�� `service.toml`���ؿ��ն˺��ȡͬһ�ļ�����������Ҫ����������Կ����Ҫ������ύ����

Ҳ�ɲ����� `CODEX_IMAGE_SECRET`������ `CODEX_IMAGE_SECRET_FILE` Ϊͬһ�� UTF-8 ��Կ�ļ��ľ���·����ֱ����Կ��������ʱ���ȣ���ֵҲ������˵��ļ���

�������д��ڣ��رռ�ֹͣ������Ҫ��̨��פʱ�������н��̹�����������ȷͬһ��ϵͳ�û�����Կ���������þ���·����ÿ�� `state` ֻ����һ��ʵ����������ء�MaiBot �����������Ϊ��������

## 4. ��Բ��

�� MaiBot ������û��� `config.toml` ����д��

```toml
[plugin]
config_version = "0.6.3"
enabled = true

[connection]
url = "http://127.0.0.1:8765/mcp"
secret = "��д���� secret.txt �е�������Կ"
allowed_users = ["qq:123456789"]
poll_seconds = 2
watch_seconds = 360
```

ֻ�в�� `connection.allowed_users` ����˭�ɷ����󣬿��б��ܾ������ˡ����� MaiBot WebUI ����Ӧ�û����豾��� `codex_image_command` ����Ȩ�޼� `send.text` / `send.image` ��������������¼��ز������������ṩ����ģʽ�� Codex �����

## 5. ��֤

��ִ�� `/��ͼ���` ��� MCP ��������Ȩ��������� Codex ��¼��Ҳ������ͼƬ����Ҫʵ�ʻ�ͼ����ʱ������һ�� `/��ͼ` ����������¼�����Ų��˶�״̬���Զ���ͼ��������ʵ����ʾ��ͼƬ��

����ʱ�����߻���δ֪��ʹ��ԭ��Ų�ѯ����Ҫ�ظ��ύ��Ҳ��Ҫͨ��ɾ�����ݿ�ǿ�����ԡ���ʷ Windows ��������� 0.6.3 ��װ�������գ��� [��֤��¼](validation.md)��
