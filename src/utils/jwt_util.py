"""
JWT Token 验证工具

参考 Java 版本的 JwtUtil 实现，用于验证和解析 JWT token
"""
import base64
from typing import Optional, Dict, Any
from enum import Enum
from dataclasses import dataclass
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from src.common.logger import get_logger

logger = get_logger()


class ValidResult(Enum):
    """Token 验证结果枚举"""
    SUCCESS = "SUCCESS"
    LOGIN_EXPIRED = "LOGIN_EXPIRED"
    NOT_LOGIN = "NOT_LOGIN"


@dataclass
class JwtValidResult:
    """
    JWT 验证结果

    对应 Java 中的 JwtValidResult 类
    """
    valid_result: ValidResult
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    qm: Optional[str] = None  # session key name
    project_id: Optional[int] = None

    @classmethod
    def of_success(
        cls,
        user_id: int,
        user_name: str,
        qm: str,
        project_id: Optional[int] = None
    ) -> "JwtValidResult":
        """创建成功结果"""
        return cls(
            valid_result=ValidResult.SUCCESS,
            user_id=user_id,
            user_name=user_name,
            qm=qm,
            project_id=project_id
        )

    @classmethod
    def of_fail(cls, result_type: ValidResult) -> "JwtValidResult":
        """创建失败结果"""
        return cls(valid_result=result_type)


class JwtUtil:
    """
    JWT Token 工具类

    参考 Java 版本的 JwtUtil 实现
    """

    # JWT 密钥（与 Java 版本保持一致）
    JWT_SECRET_KEY = "jwt#qm.Secret@213"

    @classmethod
    def _get_base64_secret_key(cls) -> bytes:
        """
        获取 Base64 编码的密钥

        与 Java 版本的 getBase64SecretKey() 方法保持一致
        """
        return base64.b64encode(cls.JWT_SECRET_KEY.encode())

    @classmethod
    def valid_token(cls, jwt_token: str) -> JwtValidResult:
        """
        验证 JWT token

        Args:
            jwt_token: JWT token 字符串

        Returns:
            JwtValidResult: 验证结果

        参考 Java 版本的 validToken() 方法：
        - SUCCESS: token 有效
        - LOGIN_EXPIRED: token 已过期
        - NOT_LOGIN: token 无效或其他错误
        """
        try:
            claims = cls.parse_token(jwt_token)

            if not claims:
                logger.warning("Token 验证失败：claims 为空")
                return JwtValidResult.of_fail(ValidResult.NOT_LOGIN)

            # 从 claims 中提取字段（与 Java 版本保持一致）
            user_id = claims.get("userId")
            user_name = claims.get("userName")
            qm = claims.get("qm")  # session key name
            project_id = claims.get("projectId")

            if not user_id:
                logger.warning("Token 验证失败：userId 为空")
                return JwtValidResult.of_fail(ValidResult.NOT_LOGIN)

            logger.info(f"Token 验证成功 | userId={user_id} | userName={user_name}")
            return JwtValidResult.of_success(user_id, user_name, qm, project_id)

        except ExpiredSignatureError:
            logger.warning("Token 已过期")
            return JwtValidResult.of_fail(ValidResult.LOGIN_EXPIRED)
        except InvalidTokenError as e:
            logger.warning(f"Token 无效 | error={e}")
            return JwtValidResult.of_fail(ValidResult.NOT_LOGIN)
        except Exception as e:
            logger.error(f"Token 验证异常 | error={e}")
            return JwtValidResult.of_fail(ValidResult.NOT_LOGIN)

    @classmethod
    def parse_token(cls, jwt_token: str) -> Optional[Dict[str, Any]]:
        """
        解析 JWT token 获取 claims

        Args:
            jwt_token: JWT token 字符串

        Returns:
            claims 字典，解析失败返回 None

        参考 Java 版本的 parseToken() 方法
        """
        try:
            # 使用 HS256 算法解析（与 Java 版本保持一致）
            payload = jwt.decode(
                jwt_token,
                cls._get_base64_secret_key(),
                algorithms=["HS256"]
            )
            return payload
        except Exception as e:
            logger.error(f"Token 解析失败 | error={e}")
            return None


class AuthConstant:
    """
    认证常量

    对应 Java 中的 AuthConstant 类
    """

    # Token header 名称
    LOGIN_TOKEN_HEAD_NAME = "Authorization"

    # Session user key
    SESSION_USER_KEY = "SessionUser"

    # Redis key 前缀
    QM_REDIS_KEY_PREFIX = "qm_auth_"

    # Session key name
    SESSION_USER_KEY_NAME = "qm"

    @staticmethod
    def get_session_user_key(user_id: int) -> str:
        """
        获取 Redis session key

        Args:
            user_id: 用户 ID

        Returns:
            Redis key: qm_auth_{userId}
        """
        return f"{AuthConstant.QM_REDIS_KEY_PREFIX}{user_id}"
