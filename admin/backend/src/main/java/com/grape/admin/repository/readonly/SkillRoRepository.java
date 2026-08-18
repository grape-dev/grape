package com.grape.admin.repository.readonly;

import com.grape.admin.entity.readonly.SkillRo;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

public interface SkillRoRepository extends JpaRepository<SkillRo, Integer> {

    /** 上传覆盖：删除同键旧记录（skill_name + scope + owner_id 唯一键） */
    @Modifying
    @Transactional
    @Query(value = "DELETE FROM skill WHERE skill_name = :name AND scope = :scope AND owner_id = :owner", nativeQuery = true)
    void deleteBySkillNameAndScopeAndOwnerId(@Param("name") String name,
                                             @Param("scope") String scope,
                                             @Param("owner") String owner);

    /** 上传入库（v2.5：file_path 真实存储路径 + created_by/updated_by 审计） */
    @Modifying
    @Transactional
    @Query(value = "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, file_path, scope, owner_id, created_by, updated_by) "
            + "VALUES (:name, :displayName, :description, :agentType, :skillPath, :filePath, :scope, :owner, :createdBy, :updatedBy)", nativeQuery = true)
    void insertSkill(@Param("name") String name,
                     @Param("displayName") String displayName,
                     @Param("description") String description,
                     @Param("agentType") String agentType,
                     @Param("skillPath") String skillPath,
                     @Param("filePath") String filePath,
                     @Param("scope") String scope,
                     @Param("owner") String owner,
                     @Param("createdBy") String createdBy,
                     @Param("updatedBy") String updatedBy);
}
