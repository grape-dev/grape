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

    /** 上传入库（字段语义与主项目 grape create_skill 一致） */
    @Modifying
    @Transactional
    @Query(value = "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) "
            + "VALUES (:name, :displayName, :description, :agentType, :skillPath, :scope, :owner)", nativeQuery = true)
    void insertSkill(@Param("name") String name,
                     @Param("displayName") String displayName,
                     @Param("description") String description,
                     @Param("agentType") String agentType,
                     @Param("skillPath") String skillPath,
                     @Param("scope") String scope,
                     @Param("owner") String owner);
}
