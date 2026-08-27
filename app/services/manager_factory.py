from app.services.xui_ssh import SSHXUIManager


managers = {}


def has_ssh_target(server_conf):
    """判断这台服务器是否具备走 SSH 引擎的条件。

    主机地址与 app/services/ssh.py 的解析规则保持一致：优先 ssh_host，
    为空时从 url 里取主机名，两者都没有才算不可用。
    """
    if not server_conf.get('probe_installed'):
        return False
    return bool(server_conf.get('ssh_host') or server_conf.get('url'))


def get_manager(server_conf):
    """返回单台服务器的节点管理器（仅 SSH 引擎）。

    节点读写统一走 SSH 直连远程 X-UI 数据库。条件不满足时直接抛出异常，
    由调用方决定如何降级展示。
    """
    if not has_ssh_target(server_conf):
        raise RuntimeError('无法管理节点：请确保该服务器已安装探针且 SSH 连接可用')

    mgr_key = f"ssh_{server_conf.get('url') or server_conf.get('ssh_host')}"

    mgr = managers.get(mgr_key)
    if mgr:
        # 复用已有实例，但要刷新配置引用，避免拿到旧的 SSH 凭据
        mgr.server_conf = server_conf
    else:
        mgr = SSHXUIManager(server_conf)
        managers[mgr_key] = mgr

    return mgr
