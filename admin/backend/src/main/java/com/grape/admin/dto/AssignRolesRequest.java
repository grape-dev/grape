package com.grape.admin.dto;

import lombok.Getter;
import lombok.Setter;

import java.util.List;

@Getter
@Setter
public class AssignRolesRequest {

    private List<Long> roleIds;
}
