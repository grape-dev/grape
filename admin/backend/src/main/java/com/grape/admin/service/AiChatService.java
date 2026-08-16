package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.entity.AdminAiChatMessage;
import com.grape.admin.entity.AdminAiChatSession;
import com.grape.admin.repository.AdminAiChatMessageRepository;
import com.grape.admin.repository.AdminAiChatSessionRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

/** AI 助手会话服务：建会话/写消息（task_id 配对——seq MAX+1——唯一约束兜底）/归属校验/游标分页 */
@Service
@RequiredArgsConstructor
public class AiChatService {

    private final AdminAiChatSessionRepository sessionRepository;
    private final AdminAiChatMessageRepository messageRepository;

    /** 创建会话（UUID——绑定 hermes --continue——name = 首个问题截断——刷新后名称正确） */
    @Transactional
    public AdminAiChatSession createSession(String createdBy, String firstQuery) {
        AdminAiChatSession s = new AdminAiChatSession();
        s.setSessionId(UUID.randomUUID().toString().replace("-", ""));
        s.setCreatedBy(createdBy);
        String name = (firstQuery == null || firstQuery.isBlank()) ? "新对话"
                : (firstQuery.length() > 50 ? firstQuery.substring(0, 50) : firstQuery);
        s.setName(name);
        return sessionRepository.save(s);
    }

    /** 会话归属校验（存在 + created_by = 当前用户——403） */
    public AdminAiChatSession checkOwnership(String sessionId, String currentUser) {
        AdminAiChatSession s = sessionRepository.findById(sessionId)
                .orElseThrow(() -> new BizException("会话不存在或无权访问"));
        if (!s.getCreatedBy().equals(currentUser)) {
            throw new BizException("会话不存在或无权访问");
        }
        return s;
    }

    /** 写消息（user/assistant——seq = MAX+1——唯一约束冲突由调用方重试/或报错） */
    @Transactional
    public AdminAiChatMessage addMessage(String sessionId, String taskId, String role, String content) {
        long seq = messageRepository.maxSeq(sessionId) + 1;
        AdminAiChatMessage m = new AdminAiChatMessage();
        m.setMsgId(UUID.randomUUID().toString().replace("-", ""));
        m.setSessionId(sessionId);
        m.setTaskId(taskId);
        m.setRole(role);
        m.setContent(content);
        if ("user".equals(role)) m.setSendStatus(1);   // 投递成功（user 专用——不标失败）
        m.setSeq(seq);
        try {
            AdminAiChatMessage saved = messageRepository.save(m);
            // 写消息后更新会话最后活动时间（列表排序——运行中会话排第一——
            // 刷新后 activeSession 指向它——重连目标正确）
            sessionRepository.findById(sessionId).ifPresent(s -> {
                s.setUpdatedAt(java.time.LocalDateTime.now());
                sessionRepository.save(s);
            });
            return saved;
        } catch (org.springframework.dao.DataIntegrityViolationException e) {
            // 唯一约束冲突（并发同 seq）——重试一次（重取 MAX+1）
            long seq2 = messageRepository.maxSeq(sessionId) + 1;
            m.setSeq(seq2);
            return messageRepository.save(m);
        }
    }

    /** 幂等写 assistant（同 task_id 已有 assistant → UPDATE 覆盖——重试场景）
     *  generate_status：2=成功 / 3=失败（失败带 error_code/error_msg——后台排查） */
    @Transactional
    public void saveAssistantAnswer(String sessionId, String taskId, String content, boolean markFailed,
                                    String errorCode, String errorMsg) {
        messageRepository.findAssistantByTask(sessionId, taskId).ifPresentOrElse(existing -> {
            existing.setContent(content);
            existing.setGenerateStatus(markFailed ? 3 : 2);
            existing.setErrorCode(markFailed ? errorCode : null);
            existing.setErrorMsg(markFailed ? errorMsg : null);
            existing.setCreateTime(LocalDateTime.now());
            messageRepository.save(existing);
        }, () -> {
            AdminAiChatMessage m = addMessage(sessionId, taskId, "assistant", content);
            m.setGenerateStatus(markFailed ? 3 : 2);
            m.setErrorCode(markFailed ? errorCode : null);
            m.setErrorMsg(markFailed ? errorMsg : null);
            messageRepository.save(m);
        });
    }

    /** 会话消息游标分页（首次 cursorSeq=null 取最新 limit 条——返回升序 + hasMore/minSeq） */
    public PageResult listMessages(String sessionId, String currentUser, Long cursorSeq, int limit) {
        checkOwnership(sessionId, currentUser);
        List<AdminAiChatMessage> rows = messageRepository.findBySessionIdOrderBySeqDesc(
                sessionId, cursorSeq, PageRequest.of(0, limit));
        boolean hasMore = rows.size() >= limit;
        // 反转（seq 升序——前端时序正常）
        java.util.Collections.reverse(rows);
        long minSeq = rows.isEmpty() ? 0 : rows.get(0).getSeq();
        return new PageResult(rows, hasMore, minSeq);
    }

    /** 会话列表（双模式：游标分页（滚动加载——有 cursorTime 用）/
     *  offset 分页（页码——无 cursor 时用——兼容后期正常分页）） */
    public List<AdminAiChatSession> listSessions(String currentUser, int limit, String cursorTime, String cursorId, int offset) {
        if (cursorTime != null && !cursorTime.isBlank()) {
            java.time.LocalDateTime ct = java.time.LocalDateTime.parse(cursorTime);
            return sessionRepository.findPageByCreatedBy(currentUser, ct, cursorId,
                    org.springframework.data.domain.PageRequest.of(0, limit + 1));
        }
        return sessionRepository.findByCreatedByOrderByUpdatedAtDesc(currentUser,
                org.springframework.data.domain.PageRequest.of(Math.max(offset, 0) / Math.max(limit, 1), limit));
    }

    /** 会话总数（当前用户） */
    public long countSessions(String currentUser) {
        return sessionRepository.countByCreatedByAndDeleted(currentUser);
    }

    /** 查库任务最终状态（status 接口——内存清理/重启后兜底——任务状态持久化）：
     *  done（assistant generate_status=2）/ error（3 或中断——user 有 assistant 无）/ null（无记录） */
    public String getTaskFinalStatus(String taskId) {
        List<AdminAiChatMessage> msgs = messageRepository.findByTaskIdOrderBySeq(taskId);
        boolean hasUser = false;
        Integer genStatus = null;
        boolean hasAssistant = false;
        for (AdminAiChatMessage m : msgs) {
            if ("user".equals(m.getRole())) hasUser = true;
            else if ("assistant".equals(m.getRole())) { hasAssistant = true; genStatus = m.getGenerateStatus(); }
        }
        if (hasAssistant && genStatus != null) return genStatus == 2 ? "done" : "error";
        if (hasUser && !hasAssistant) return "error";   // 中断（assistant 未写——重启/异常）
        return null;
    }

    /** 重命名（归属校验） */
    @Transactional
    public void renameSession(String sessionId, String currentUser, String name) {
        AdminAiChatSession s = checkOwnership(sessionId, currentUser);
        s.setName(name);
        sessionRepository.save(s);
    }

    /** 删除会话（软删会话 + 消息——归属校验） */
    @Transactional
    public void deleteSession(String sessionId, String currentUser) {
        checkOwnership(sessionId, currentUser);
        AdminAiChatSession s = sessionRepository.findById(sessionId).orElseThrow();
        s.setDeleted(1);
        sessionRepository.save(s);
        messageRepository.softDeleteBySession(sessionId);
    }

    /** 分页结果（JDK11——普通类替代 record） */
    public static class PageResult {
        public final List<AdminAiChatMessage> messages;
        public final boolean hasMore;
        public final long minSeq;

        public PageResult(List<AdminAiChatMessage> messages, boolean hasMore, long minSeq) {
            this.messages = messages;
            this.hasMore = hasMore;
            this.minSeq = minSeq;
        }
    }
}
