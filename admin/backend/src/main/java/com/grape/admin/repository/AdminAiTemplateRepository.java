package com.grape.admin.repository;

import com.grape.admin.entity.AdminAiTemplate;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AdminAiTemplateRepository extends JpaRepository<AdminAiTemplate, Long> {
    List<AdminAiTemplate> findByCreatedByAndDeletedOrderByIdDesc(String createdBy, Integer deleted);
    long countByCreatedByAndDeleted(String createdBy, Integer deleted);
    boolean existsByCreatedByAndTemplateNameAndDeleted(String createdBy, String templateName, Integer deleted);
}