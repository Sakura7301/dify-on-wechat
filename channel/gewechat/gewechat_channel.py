
import os
import io
import cv2
import time
import json
import web
import uuid
import requests
from urllib.parse import urlparse
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.gewechat.gewechat_message import GeWeChatMessage
from common.log import logger
from common.singleton import singleton
from common.tmp_dir import TmpDir
from config import conf, save_config
from lib.gewechat import GewechatClient
from voice.audio_convert import mp3_to_silk


MAX_UTF8_LEN = 2048


@singleton
class GeWeChatChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()

        self.base_url = conf().get("gewechat_base_url")
        if not self.base_url:
            logger.error("[gewechat] base_url is not set")
            return
        self.token = conf().get("gewechat_token")
        self.client = GewechatClient(self.base_url, self.token)

        # 如果token为空，尝试获取token
        if not self.token:
            logger.warning("[gewechat] token is not set，trying to get token")
            token_resp = self.client.get_token()
            # {'ret': 200, 'msg': '执行成功', 'data': 'tokenxxx'}
            if token_resp.get("ret") != 200:
                logger.error(f"[gewechat] get token failed: {token_resp}")
                return
            self.token = token_resp.get("data")
            conf().set("gewechat_token", self.token)
            save_config()
            logger.info(f"[gewechat] new token saved: {self.token}")
            self.client = GewechatClient(self.base_url, self.token)

        self.app_id = conf().get("gewechat_app_id")
        if not self.app_id:
            logger.warning("[gewechat] app_id is not set，trying to get new app_id when login")

        self.download_url = conf().get("gewechat_download_url")
        if not self.download_url:
            logger.warning("[gewechat] download_url is not set, unable to download image")

        logger.info(f"[gewechat] init: base_url: {self.base_url}, token: {self.token}, app_id: {self.app_id}, download_url: {self.download_url}")

    def startup(self):
        # 如果app_id为空或登录后获取到新的app_id，保存配置
        app_id, error_msg = self.client.login(self.app_id)
        if error_msg:
            logger.error(f"[gewechat] login failed: {error_msg}")
            return

        # 如果原来的self.app_id为空或登录后获取到新的app_id，保存配置
        if not self.app_id or self.app_id != app_id:
            conf().set("gewechat_app_id", app_id)
            save_config()
            logger.info(f"[gewechat] new app_id saved: {app_id}")
            self.app_id = app_id

        # 获取回调地址，示例地址：http://172.17.0.1:9919/v2/api/callback/collect
        callback_url = conf().get("gewechat_callback_url")
        if not callback_url:
            logger.error("[gewechat] callback_url is not set, unable to start callback server")
            return

        # 创建新线程设置回调地址
        import threading
        def set_callback():
            # 等待服务器启动（给予适当的启动时间）
            import time
            logger.info("[gewechat] sleep 3 seconds waiting for server to start, then set callback")
            time.sleep(3)

            # 设置回调地址，{ "ret": 200, "msg": "操作成功" }
            callback_resp = self.client.set_callback(self.token, callback_url)
            if callback_resp.get("ret") != 200:
                logger.error(f"[gewechat] set callback failed: {callback_resp}")
                return
            logger.info("[gewechat] callback set successfully")

        callback_thread = threading.Thread(target=set_callback, daemon=True)
        callback_thread.start()

        # 从回调地址中解析出端口与url path，启动回调服务器
        parsed_url = urlparse(callback_url)
        path = parsed_url.path
        # 如果没有指定端口，使用默认端口80
        port = parsed_url.port or 80
        logger.info(f"[gewechat] start callback server: {callback_url}, using port {port}")
        urls = (path, "channel.gewechat.gewechat_channel.Query")
        app = web.application(urls, globals(), autoreload=False)
        web.httpserver.runsimple(app.wsgifunc(), ("0.0.0.0", port))

    def get_video_info(self, video_path):
        # 打开视频文件
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print("无法打开视频文件")
            return None, None
        # 获取视频的帧率
        fps = cap.get(cv2.CAP_PROP_FPS)
        # 获取视频的总帧数
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        # 计算视频时长（秒）
        duration = total_frames / fps

        # 读取第一帧
        ret, first_frame = cap.read()
        if not ret:
            print("无法读取视频帧")
            return None, None

        # 释放视频对象
        cap.release()

        return first_frame, duration

    def _download_image_from_url(self, img_url):
        """
        从URL下载图片并保存为临时文件

        Args:
            img_url: 图片URL

        Returns:
            临时文件路径
        """
        try:
            import requests

            # 下载图片
            response = requests.get(img_url, stream=True, timeout=10)
            response.raise_for_status()

            # 处理WebP格式
            if ".webp" in img_url:
                from common.utils import convert_webp_to_png
                image_data = convert_webp_to_png(response.content)
            else:
                image_data = response.content

            # 生成临时文件名
            extension = ".png" if ".webp" in img_url else self._detect_image_type(None, image_data)
            temp_file_path = os.path.join(
                TmpDir().path(),
                f"img_{uuid.uuid4()}{extension}"
            )

            # 保存图片
            with open(temp_file_path, "wb") as f:
                f.write(image_data)

            return temp_file_path

        except Exception as e:
            logger.error(f"[gewechat] 图片下载失败: {e}")
            return None

    def _save_image_from_io(self, image_file):
        """
        保存图片IO句柄为临时文件

        Args:
            image_file: 图片IO句柄

        Returns:
            临时文件路径
        """
        try:
            # 重置文件指针
            image_file.seek(0)

            # 读取图片数据
            image_data = image_file.read()

            # 关闭传入的句柄
            image_file.close()

            # 检测图片类型
            extension = self._detect_image_type(None, image_data)

            # 生成临时文件名
            temp_file_path = os.path.join(
                TmpDir().path(),
                f"img_{uuid.uuid4()}{extension}"
            )

            # 保存图片
            with open(temp_file_path, "wb") as f:
                f.write(image_data)

            return temp_file_path

        except Exception as e:
            logger.error(f"[gewechat] 图片保存失败: {e}")
            return None

    def _detect_image_type(self, file_path=None, image_data=None):
        """
        检测图片类型

        Args:
            file_path: 文件路径（可选）
            image_data: 图片数据（可选）

        Returns:
            文件扩展名
        """
        try:
            # 优先使用文件路径
            if file_path:
                with open(file_path, 'rb') as f:
                    header = f.read(6)
            # 使用图片数据
            elif image_data:
                header = image_data[:6]
            else:
                return ".png"

            return ".gif" if header.startswith((b'GIF87a', b'GIF89a')) else ".png"

        except Exception as e:
            logger.error(f"[gewechat] 图片类型检测失败: {e}")
            return ".png"

    def send(self, reply: Reply, context: Context):
        receiver = context["receiver"]
        gewechat_message = context.get("msg")
        if reply.type in [ReplyType.TEXT, ReplyType.ERROR, ReplyType.INFO]:
            reply_text = reply.content
            ats = ""
            if gewechat_message and gewechat_message.is_group:
                ats = gewechat_message.actual_user_id
            self.client.post_text(self.app_id, receiver, reply_text, ats)
            logger.info("[gewechat] Do send text to {}: {}".format(receiver, reply_text))
        elif reply.type == ReplyType.VOICE:
            # 发送语音
            self.send_voice(reply, receiver)
        # 判断回复消息类型是图片URL或图片类型
        elif reply.type == ReplyType.IMAGE_URL or reply.type == ReplyType.IMAGE:
            # 发送图片
            self.send_image(reply, receiver)
        elif reply.type == ReplyType.VIDEO_URL:
            # 发送视频
            self.send_video(reply, receiver)

    def send_voice(self, reply, receiver):
        """
        发送语音消息

        Args:
            reply: 回复消息对象
            receiver: 接收者
        """
        # 临时文件路径
        silk_path = None

        try:
            content = reply.content

            # 检查文件类型
            if not content.endswith('.mp3'):
                logger.error(f"[gewechat] 语音文件不是MP3格式: {content}")
                return

            # 生成唯一的silk文件名
            silk_path = f"{content}_{uuid.uuid4()}.silk"

            # 转换MP3到silk格式
            try:
                duration = mp3_to_silk(content, silk_path)
            except Exception as e:
                logger.error(f"[gewechat] MP3转换失败: {e}")
                return

            # 获取回调URL
            callback_url = conf().get("gewechat_callback_url")
            if not callback_url:
                logger.error("[gewechat] 未配置回调URL")
                return

            # 构建silk文件URL
            silk_url = f"{callback_url}?file={silk_path}"

            # 发送语音
            try:
                res = self.client.post_voice(
                    self.app_id,
                    receiver,
                    silk_url,
                    duration
                )

                # 检查发送结果
                if res.get('ret') != 200:
                    logger.error(f"[gewechat] 语音发送失败: {res}")
                    return

                logger.info(
                    f"[gewechat] 发送语音成功: 接收者={receiver}, URL={silk_url}, 时长={duration/1000.0}秒"
                )

            except Exception as e:
                logger.error(f"[gewechat] 语音发送异常: {e}")

        except Exception as e:
            logger.error(f"[gewechat] 语音处理异常: {e}")

        finally:
            # 删除临时文件
            self._delete_temp_file(silk_path)

    def send_image(self, reply, receiver):
            # 临时文件路径
        temp_file_path = None

        try:
            # 处理图片URL
            if reply.type == ReplyType.IMAGE_URL:
                # 下载并保存图片
                temp_file_path = self._download_image_from_url(reply.content)

            # 处理图片IO句柄
            elif reply.type == ReplyType.IMAGE:
                # 保存图片IO句柄为临时文件
                temp_file_path = self._save_image_from_io(reply.content)

            else:
                logger.warning(f"[gewechat] 未知的图片类型: {reply.type}")
                return

            # 检查临时文件是否成功创建
            if not temp_file_path or not os.path.exists(temp_file_path):
                logger.error("[gewechat] 图片保存失败")
                return

            # 检测图片类型
            extension = self._detect_image_type(temp_file_path)

            # 构建回调URL
            callback_url = conf().get("gewechat_callback_url")
            img_url = f"{callback_url}?file={temp_file_path}"

            # 发送图片
            if extension == ".gif":
                result = self.client.post_file(
                    self.app_id,
                    receiver,
                    file_url=img_url,
                    file_name=os.path.basename(temp_file_path)
                )
            else:
                result = self.client.post_image(
                    self.app_id,
                    receiver,
                    img_url
                )

            # 检查发送结果
            if result.get('ret') != 200:
                logger.error(f"[gewechat] 图片发送失败: {result}")
                return

            logger.debug(f"[gewechat] 图片发送成功: {temp_file_path}")

        except Exception as e:
            logger.error(f"[gewechat] 图片发送异常: {e}")

        finally:
            # 删除临时文件
            self._delete_temp_file(temp_file_path)

    def send_video(self, reply, receiver):
        """
        发送视频的主方法

        :param reply: 回复对象，包含视频URL
        :param receiver: 接收者
        """
        try:
            if reply.type != ReplyType.VIDEO_URL:
                logger.warning(f"非视频类型消息: {reply.type}")
                return

            video_url = reply.content
            logger.debug(f"准备发送视频: {video_url}")

            # 获取视频信息
            first_frame, duration = self.get_video_info(video_url)
            if first_frame is None or duration is None:
                logger.error("无法获取视频信息")
                return

            # 创建临时目录
            tmp_dir = os.path.join(os.getcwd(), 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)

            # 使用UUID生成唯一文件名
            unique_filename = f"{uuid.uuid4()}.jpg"
            img_path = os.path.join(tmp_dir, unique_filename)

            try:
                # 保存第一帧图片
                cv2.imwrite(img_path, first_frame)
                logger.debug(f"第一帧图片已保存: {img_path}")

                # 获取回调URL
                callback_url = conf().get("gewechat_callback_url")
                relative_img_path = os.path.relpath(img_path, os.getcwd())
                img_url = f"{callback_url}?file={relative_img_path}"

                # 发送视频
                res = self.client.post_video(
                    self.app_id,
                    receiver,
                    video_url,
                    img_url,
                    int(duration)
                )
                logger.info(f"视频发送成功: receiver={receiver}, duration={duration}")

            except Exception as e:
                logger.error(f"视频发送过程中出错: {e}")

            finally:
                # 删除临时文件
                self._delete_temp_file(img_path)

        except Exception as e:
            logger.error(f"视频处理发生异常: {e}")

    def get_video_info(self, video_path):
        """
        获取视频基本信息

        :param video_path: 视频文件路径
        :return: 第一帧图像和视频时长
        """
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                logger.error(f"无法打开视频文件: {video_path}")
                return None, None

            # 获取视频帧率和总帧数
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            # 计算视频时长
            duration = total_frames / fps

            # 读取第一帧
            ret, first_frame = cap.read()
            cap.release()

            if not ret:
                logger.error("无法读取视频第一帧")
                return None, None

            return first_frame, duration

        except Exception as e:
            logger.error(f"获取视频信息时发生异常: {e}")
            return None, None

    def _delete_temp_file(self, file_path):
        """
        安全删除临时文件

        :param file_path: 要删除的文件路径
        """
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"已删除临时文件: {file_path}")
        except Exception as e:
            logger.error(f"删除临时文件时发生错误: {e}")


class Query:
    def GET(self):
        # 搭建简单的文件服务器，用于向gewechat服务传输语音等文件，但只允许访问tmp目录下的文件
        params = web.input(file="")
        file_path = params.file
        if file_path:
            # 使用绝对路径
            clean_path = os.path.abspath(os.path.join(os.getcwd(), file_path))
            # 获取tmp目录的绝对路径
            tmp_dir = os.path.abspath(os.path.join(os.getcwd(), 'tmp'))

            # 检查文件路径是否在tmp目录下
            if not clean_path.startswith(tmp_dir):
                logger.error(f"[gewechat] Forbidden access to file outside tmp directory: file_path={file_path}, clean_path={clean_path}, tmp_dir={tmp_dir}")
                raise web.forbidden()

            if os.path.exists(clean_path):
                with open(clean_path, 'rb') as f:
                    return f.read()
            else:
                logger.error(f"[gewechat] File not found: {clean_path}")
                raise web.notfound()
        return "gewechat callback server is running"

    def POST(self):
        channel = GeWeChatChannel()
        web_data = web.data()
        logger.debug("[gewechat] receive data: {}".format(web_data))
        data = json.loads(web_data)

        # gewechat服务发送的回调测试消息
        if isinstance(data, dict) and 'testMsg' in data and 'token' in data:
            logger.debug(f"[gewechat] 收到gewechat服务发送的回调测试消息")
            return "success"

        gewechat_msg = GeWeChatMessage(data, channel.client)

        # 微信客户端的状态同步消息
        if gewechat_msg.ctype == ContextType.STATUS_SYNC:
            logger.debug(f"[gewechat] ignore status sync message: {gewechat_msg.content}")
            return "success"

        # 忽略非用户消息（如公众号、系统通知等）
        if gewechat_msg.ctype == ContextType.NON_USER_MSG:
            logger.debug(f"[gewechat] ignore non-user message from {gewechat_msg.from_user_id}: {gewechat_msg.content}")
            return "success"

        # 忽略来自自己的消息
        if gewechat_msg.my_msg:
            logger.debug(f"[gewechat] ignore message from myself: {gewechat_msg.actual_user_id}: {gewechat_msg.content}")
            return "success"

        # 忽略过期的消息
        if int(gewechat_msg.create_time) < int(time.time()) - 60 * 5: # 跳过5分钟前的历史消息
            logger.debug(f"[gewechat] ignore expired message from {gewechat_msg.actual_user_id}: {gewechat_msg.content}")
            return "success"

        context = channel._compose_context(
            gewechat_msg.ctype,
            gewechat_msg.content,
            isgroup=gewechat_msg.is_group,
            msg=gewechat_msg,
        )
        if context:
            channel.produce(context)
        return "success"
