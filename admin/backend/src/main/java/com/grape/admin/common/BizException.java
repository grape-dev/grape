package com.grape.admin.common;

/** 业务错误 → 400 */
public class BizException extends RuntimeException {
    public BizException(String message) {
        super(message);
    }
}
